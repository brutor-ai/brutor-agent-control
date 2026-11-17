/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * AI Systems fleet board.
 *
 * One place to see every governed AI System: what kind it is, who owns it,
 * where it is in its lifecycle, what it is made of, and — the part that
 * earns the board its keep before the run ledger exists — what governance
 * documentation is missing.
 *
 * Each row carries the assurance stripe (same ladder as the Mission
 * Control cards — the two surfaces must never disagree about which
 * systems need attention), read from the materialised health rollup.
 * A system with no judgment renders gray, never green: absence of
 * evidence is not health.
 */

import { createElement, useCallback, useEffect, useRef, useState } from 'react';
import { TABLE_TH_TYPE } from '../ui/tableStyles';
import {
    AlertTriangle,
    ArrowLeft,
    ArrowRight,
    ArrowUpRight,
    Boxes,
    Check,
    ChevronDown,
    HelpCircle,
    LayoutGrid,
    List,
    RefreshCw,
    ShieldCheck,
    ShieldQuestion,
} from 'lucide-react';

import { HelpTip } from '../ui/HelpTip';
import { AnchoredPopover } from '../ui/AnchoredPopover';
import { useGateway } from '../../hooks/useGateway';
import {
    AI_SYSTEM_ROLE_FILTER_LABELS,
    AI_SYSTEM_ROLE_LABELS,
    AISystemFleetResponse,
    AISystemRoleFilter,
    AISystemCardFacts,
    AISystemCardKpis,
    AISystemSummary,
    assuranceBorder,
    assuranceStripe,
    assuranceTone,
    type AssuranceTone,
    HEALTH_STYLE,
    VERDICT_STYLE,
    DEPENDENCY_HEALTH_CLASSES,
    DEPENDENCY_HEALTH_LABELS,
    GOVERNANCE_GAP_LABELS,
} from '../../lib/aiSystemTypes';
import { effectiveTierText } from '../../lib/composition';
import AISystemRunsTab from './AISystemRunsTab';
import { LivenessBadge } from './LivenessBadge';
import {
    GroupType,
    LIFECYCLE_STAGE_LABELS,
    SystemKind,
    SYSTEM_KIND_LABELS,
    systemKindLabel,
} from '../../lib/groupTypes';
import { resolveGroupIcon } from '../../lib/lucideIconMap';
import { errorKind } from '../../lib/narrow';

interface AISystemsFleetTabProps {
    /** Deep link: open directly on one system's detail view. */
    initialDrilldownId?: string;
    /** Which detail tab the deep link lands on (e.g. 'drift'). */
    initialSection?:
        | 'composition'
        | 'lifecycle'
        | 'signals'
        | 'runs'
        | 'trajectory'
        | 'drift'
        | 'contract'
        | 'compliance'
        | 'assurance';
    /** Lets the drilldown link out of the fleet — the contract lifecycle
     *  gate uses it to reach the group's Ownership & Purpose fields. */
    setActiveTab?: (tab: string) => void;
    /** The run the route applies to the deep-linked system's Composition
     *  graph (`ai-systems:{id}:composition:{runId}`). */
    routeRunId?: string | null;
}

/** The system whose runs are being viewed, if any. */
interface RunsDrilldown {
    id: string;
    name: string;
    /** agent / assistant / workflow. The fleet list shows it in its own
     *  column, but the drilldown header showed only the name — so the one
     *  fact that changes how every signal below should be read (a workflow
     *  and an agent are not judged the same way) disappeared on the way in. */
    kind?: string | null;
}

const RISK_TIER_CLASSES: Record<string, string> = {
    high: 'bg-red-100 text-red-800',
    unacceptable: 'bg-red-200 text-red-900',
    limited: 'bg-amber-100 text-amber-800',
    minimal: 'bg-emerald-100 text-emerald-800',
};

const LIFECYCLE_CLASSES: Record<string, string> = {
    proposed: 'bg-slate-100 text-slate-700',
    approved: 'bg-blue-100 text-blue-800',
    active: 'bg-emerald-100 text-emerald-800',
    deprecated: 'bg-amber-100 text-amber-800',
    retired: 'bg-slate-200 text-slate-600',
};

/** How the fleet is laid out. A per-viewer convenience, so browser storage
 *  is enough — and every access is guarded, because storage can be blocked. */
type FleetView = 'table' | 'gallery';
const VIEW_STORAGE_KEY = 'aiSystemsFleetView';

function readStoredView(): FleetView {
    try {
        // Gallery is the default; only an explicit table choice sticks.
        return localStorage.getItem(VIEW_STORAGE_KEY) === 'table' ? 'table' : 'gallery';
    } catch {
        return 'gallery';
    }
}

export function AISystemsFleetTab({ initialDrilldownId, initialSection, setActiveTab, routeRunId }: AISystemsFleetTabProps) {
    const { fetchAISystems } = useGateway();

    const [data, setData] = useState<AISystemFleetResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [kindFilter, setKindFilter] = useState('');
    const [stageFilter, setStageFilter] = useState('');
    const [roleFilter, setRoleFilter] = useState<AISystemRoleFilter | ''>('');
    const [undocumentedOnly, setUndocumentedOnly] = useState(false);
    const [view, setViewState] = useState<FleetView>(readStoredView);
    const setView = (next: FleetView) => {
        setViewState(next);
        try {
            localStorage.setItem(VIEW_STORAGE_KEY, next);
        } catch {
            /* the choice still applies for this visit */
        }
    };
    const [drilldown, setDrilldown] = useState<RunsDrilldown | null>(
        initialDrilldownId ? { id: initialDrilldownId, name: '' } : null
    );

    const load = useCallback(async () => {
        try {
            setLoading(true);
            setError('');
            const response = await fetchAISystems({
                system_kind: kindFilter || undefined,
                lifecycle_stage: stageFilter || undefined,
                undocumented_only: undocumentedOnly || undefined,
                role: roleFilter || undefined,
                // The row stripe needs the pinned judgments (health +
                // verdict) from the materialised rollup — one grouped
                // read, never a per-system recompute.
                include_health: true,
                // The gallery card's agent / gateway / KPI facts — batched
                // server-side, and only fetched when cards are on screen.
                include_card: view === 'gallery' || undefined,
            });
            setData(response);
        } catch (err: unknown) {
            console.error('Failed to load AI Systems:', errorKind(err));
            setError(err instanceof Error ? err.message : 'Failed to load AI Systems');
            setData(null);
        } finally {
            setLoading(false);
        }
    }, [fetchAISystems, kindFilter, stageFilter, roleFilter, undocumentedOnly, view]);

    useEffect(() => {
        void load();
    }, [load]);


    const systems = data?.systems ?? [];

    // A deep link arrives with only the id; fill the display name in
    // once the fleet payload knows it.
    useEffect(() => {
        if (drilldown && !drilldown.name) {
            const match = systems.find((s) => s.id === drilldown.id);
            if (match) {
                setDrilldown({
                    id: drilldown.id,
                    name: match.display_name || match.name,
                    kind: match.system_kind,
                });
            }
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [systems, drilldown?.id, drilldown?.name]);

    if (drilldown) {
        return (
            <div className="p-6 space-y-4" data-testid="ai-systems-runs-drilldown">
                {/* The identity line is passed INTO the runs tab rather than
                    rendered above it, so the system's name and its current
                    verdicts occupy one row instead of two. `min-w-0` keeps the
                    name truncating rather than shoving the strip off-screen on
                    a narrow viewport. */}
                <AISystemRunsTab
                    aiSystemId={drilldown.id}
                    initialSection={initialSection}
                    setActiveTab={setActiveTab}
                    routeRunId={drilldown.id === initialDrilldownId ? routeRunId : null}
                    identity={
                        <div className="flex items-center gap-3 min-w-0">
                            <button
                                type="button"
                                onClick={() => setDrilldown(null)}
                                className="flex items-center gap-1 text-sm text-secondary hover:text-default shrink-0"
                            >
                                <ArrowLeft className="w-4 h-4" />
                                All AI Systems
                            </button>
                            <span className="text-muted shrink-0">/</span>
                            <h2 className="text-lg font-semibold text-default truncate">
                                {drilldown.name}
                            </h2>
                            {/* Kind as a chip rather than "{name} ({kind})":
                                the name is what truncates on a narrow viewport,
                                and a parenthesised suffix would be the first
                                thing cut off. `shrink-0` keeps it. */}
                            {drilldown.kind && (
                                <span
                                    className="shrink-0 px-2 py-0.5 rounded text-xs font-medium bg-surface-raised text-secondary border border-default"
                                    data-testid="drilldown-system-kind"
                                >
                                    {systemKindLabel(drilldown.kind)}
                                </span>
                            )}
                            <HelpTip
                                title="What this page shows"
                                text="What this system did, and the expectations set on it. Sections marked 'observed' are read-only measurements taken from the run ledger. Cards marked 'editable' — the liveness expectation, the operating envelope — are yours to configure, and the envelope's enforced ceilings are the only settings here that refuse traffic."
                            />
                        </div>
                    }
                />
            </div>
        );
    }

    return (
        <div className="p-6 space-y-6" data-testid="ai-systems-fleet">
            {/* Header */}
            <div className="flex items-start justify-between gap-4">
                <div>
                    <h2 className="text-xl font-semibold text-default">AI Systems</h2>
                    <p className="text-sm text-muted mt-1">
                        Every governed AI System in this tenant. An AI System is a resource
                        group that composes the models, servers and skills a deployed piece
                        of AI actually uses.
                    </p>
                </div>
                <button
                    type="button"
                    onClick={() => void load()}
                    className="flex items-center gap-2 px-3 py-2 text-sm border border-strong rounded-lg hover:bg-surface-hover"
                >
                    <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
                    Refresh
                </button>
            </div>

            {/* Filters */}
            <div className="flex flex-wrap items-center gap-3">
                <select
                    aria-label="Filter by kind"
                    value={kindFilter}
                    onChange={(e) => setKindFilter(e.target.value)}
                    className="px-3 py-2 border border-strong rounded-lg bg-surface text-sm"
                >
                    <option value="">All kinds</option>
                    {Object.values(SystemKind).map((kind) => (
                        <option key={kind} value={kind}>
                            {SYSTEM_KIND_LABELS[kind]}
                        </option>
                    ))}
                </select>

                <select
                    aria-label="Filter by lifecycle stage"
                    value={stageFilter}
                    onChange={(e) => setStageFilter(e.target.value)}
                    className="px-3 py-2 border border-strong rounded-lg bg-surface text-sm"
                >
                    <option value="">All stages</option>
                    {Object.entries(LIFECYCLE_STAGE_LABELS).map(([stage, label]) => (
                        <option key={stage} value={stage}>
                            {label}
                        </option>
                    ))}
                </select>

                {/* Where the system sits in the composition. */}
                <select
                    aria-label="Filter by role"
                    value={roleFilter}
                    onChange={(e) => setRoleFilter(e.target.value as AISystemRoleFilter | '')}
                    className="px-3 py-2 border border-strong rounded-lg bg-surface text-sm"
                >
                    <option value="">All roles</option>
                    {(Object.keys(AI_SYSTEM_ROLE_FILTER_LABELS) as AISystemRoleFilter[]).map((r) => (
                        <option key={r} value={r}>
                            {AI_SYSTEM_ROLE_FILTER_LABELS[r]}
                        </option>
                    ))}
                </select>

                <label className="flex items-center gap-2 text-sm text-secondary">
                    <input
                        type="checkbox"
                        checked={undocumentedOnly}
                        onChange={(e) => setUndocumentedOnly(e.target.checked)}
                        className="rounded border-strong"
                    />
                    Only systems with governance gaps
                </label>

                <div className="ml-auto flex items-center gap-1 bg-surface-raised rounded-lg p-1" role="group" aria-label="Layout">
                    {([
                        ['table', List, 'Table view'],
                        ['gallery', LayoutGrid, 'Gallery view'],
                    ] as const).map(([key, ViewIcon, label]) => (
                        <button
                            key={key}
                            type="button"
                            onClick={() => setView(key)}
                            aria-pressed={view === key}
                            aria-label={label}
                            title={label}
                            data-testid={`fleet-view-${key}`}
                            className={`p-2 rounded transition-colors ${
                                view === key
                                    ? 'bg-surface text-primary-600 dark:text-primary-400 shadow-sm'
                                    : 'text-secondary hover:text-default'
                            }`}
                        >
                            <ViewIcon className="w-4 h-4" />
                        </button>
                    ))}
                </div>
            </div>

            {error && (
                <div className="flex items-start gap-3 p-4 border border-red-200 bg-red-50 rounded-lg">
                    <AlertTriangle className="w-5 h-5 text-red-600 shrink-0 mt-0.5" />
                    <span className="text-sm text-red-800">{error}</span>
                </div>
            )}

            {/* Table.
                Note the `!error` guard on the empty state: a failed fetch
                leaves `systems` empty, and rendering "No AI Systems yet"
                underneath an error banner would tell an operator with a
                fleet of forty that they have none. On failure the error
                stands alone. */}
            {loading && !data ? (
                <div className="p-8 text-center text-muted text-sm">Loading AI Systems…</div>
            ) : error ? null : systems.length === 0 ? (
                <EmptyState filtered={Boolean(kindFilter || stageFilter || roleFilter || undocumentedOnly)} />
            ) : view === 'gallery' ? (
                <div className="grid gap-4 items-start grid-cols-[repeat(auto-fill,minmax(min(100%,42rem),1fr))]" data-testid="fleet-gallery">
                    {systems.map((system) => (
                        <AISystemCard
                            key={system.id}
                            system={system}
                            onOpen={() =>
                                setDrilldown({
                                    id: system.id,
                                    name: system.display_name || system.name,
                                    kind: system.system_kind,
                                })
                            }
                        />
                    ))}
                </div>
            ) : (
                <div className="overflow-x-auto border border-default rounded-lg">
                    <table className="min-w-full divide-y divide-default">
                        <thead className="bg-surface-alt border-b border-default">
                            <tr>
                                <Th>System</Th>
                                <Th info={HEADER_INFO.kind}>Kind</Th>
                                <Th info={HEADER_INFO.owner}>Owner</Th>
                                <Th info={HEADER_INFO.lifecycle}>Lifecycle</Th>
                                <Th info={HEADER_INFO.risk}>Risk</Th>
                                <Th info={HEADER_INFO.composition}>Composition</Th>
                                <Th info={HEADER_INFO.governance}>Governance</Th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-default">
                            {systems.map((system) => (
                                <AISystemRow
                                    key={system.id}
                                    system={system}
                                    onOpen={() =>
                                        setDrilldown({
                                            id: system.id,
                                            name: system.display_name || system.name,
                                            kind: system.system_kind,
                                        })
                                    }
                                />
                            ))}
                        </tbody>
                    </table>
                </div>
            )}
        </div>
    );
}

/** Literal tone classes for the gallery card (Tailwind scans literals). */
const TONE_TEXT: Record<AssuranceTone, string> = {
    red: 'text-red-600 dark:text-red-400',
    orange: 'text-orange-600 dark:text-orange-400',
    yellow: 'text-yellow-700 dark:text-yellow-300',
    green: 'text-green-700 dark:text-green-400',
    blue: 'text-blue-600 dark:text-blue-300',
    none: 'text-muted',
};
const TONE_PILL: Record<AssuranceTone, string> = {
    red: 'border-red-500/60 bg-red-500/10 text-red-700 dark:text-red-300',
    orange: 'border-orange-500/60 bg-orange-500/10 text-orange-700 dark:text-orange-300',
    yellow: 'border-yellow-500/60 bg-yellow-500/10 text-yellow-800 dark:text-yellow-300',
    green: 'border-green-500/60 bg-green-500/10 text-green-800 dark:text-green-300',
    blue: 'border-blue-400/60 bg-blue-400/10 text-blue-700 dark:text-blue-300',
    none: 'border-default bg-surface-subtle text-muted',
};
const TONE_DOT: Record<AssuranceTone, string> = {
    red: 'bg-red-500',
    orange: 'bg-orange-500',
    yellow: 'bg-yellow-500',
    green: 'bg-green-500',
    blue: 'bg-blue-400',
    none: 'bg-gray-400',
};
/** The one-phrase reading of the tone — the card's headline judgment. It
 *  restates the ladder, never adds to it: green is earned only by healthy
 *  AND assured, and an unjudged system says so instead of looking fine. */
const TONE_SUMMARY: Record<AssuranceTone, string> = {
    red: 'Needs action',
    orange: 'Needs attention',
    yellow: 'With exceptions',
    green: 'Within policy',
    blue: 'Still learning',
    none: 'Not yet judged',
};

const MONO_LABEL = 'font-mono text-[10px] uppercase tracking-widest text-muted';

/**
 * Gallery card — the same facts as a table row, laid out as the governed
 * execution path: the agent, the gateway in the middle enforcing, and the
 * resources it may reach — then the observability and evaluation signals
 * an operator checks first. The outline uses the shared assurance ladder,
 * so a card and a Mission Control card for the same system are the same
 * colour. Card facts arrive with `include_card`; until they do (or if the
 * fetch omitted them) the boxes fall back to the composition counts.
 */
function AISystemCard({ system, onOpen }: { system: AISystemSummary; onOpen: () => void }) {
    const [expanded, setExpanded] = useState(false);
    const tone = assuranceTone(system.health_state, system.verdict_status);
    const m = system.composition.members;
    const c = system.composition;
    const facts = system.card ?? null;
    const findingCount = system.composition_finding_count ?? 0;
    const gaps = system.governance_gaps;
    const documented = gaps.length === 0 && findingCount === 0;
    const health = system.health_state ? HEALTH_STYLE[system.health_state] : undefined;
    const verdict = system.verdict_status ? VERDICT_STYLE[system.verdict_status] : undefined;
    const effectiveTier = system.risk_tier_effective ?? system.eu_ai_act_risk_tier ?? null;
    const tierLifted = Boolean(
        system.risk_tier_effective && system.risk_tier_effective !== system.eu_ai_act_risk_tier,
    );
    const kind = systemKindLabel(system.system_kind) || 'AI System';
    const stage = system.lifecycle_stage
        ? LIFECYCLE_STAGE_LABELS[system.lifecycle_stage] ?? system.lifecycle_stage
        : 'stage not set';
    const kindIcon = createElement(resolveGroupIcon(undefined, GroupType.AI_SYSTEM, system.system_kind), {
        className: 'w-3.5 h-3.5 shrink-0',
    });
    const res = facts?.resources ?? {
        models: m.models,
        mcp_servers: m.mcp_servers,
        skills: m.skills,
        delegate_agents: null,
    };

    const name = system.display_name || system.name;
    const bodyId = `ai-system-card-body-${system.id}`;

    return (
        <div
            data-testid={`ai-system-card-${system.id}`}
            data-tone={tone}
            data-expanded={expanded ? 'true' : 'false'}
            className={`flex flex-col rounded-xl border-2 bg-surface transition-shadow hover:shadow-lg ${assuranceBorder(system.health_state, system.verdict_status)}`}
        >
            <div className={`p-5 flex-1 ${expanded ? 'space-y-4' : ''}`}>
                {/* Identity — the header is the toggle. Collapsed is the
                    default so a large estate scans as a list of verdicts;
                    the drilldown has its own button (never nested inside
                    the toggle — nested buttons are invalid). */}
                <div
                    className="flex items-start justify-between gap-3 cursor-pointer"
                    onClick={() => setExpanded((v) => !v)}
                >
                    <div className="min-w-0 flex-1">
                        <button
                            type="button"
                            aria-expanded={expanded}
                            aria-controls={bodyId}
                            onClick={(e) => {
                                e.stopPropagation();
                                setExpanded((v) => !v);
                            }}
                            className="block w-full text-left rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500"
                            data-testid={`card-toggle-${system.id}`}
                        >
                            <span className={`block ${MONO_LABEL}`}>
                                {kind} / {stage}
                            </span>
                            <span className="mt-1 flex items-center gap-1.5">
                                <ChevronDown
                                    className={`w-4 h-4 text-muted shrink-0 transition-transform ${expanded ? '' : '-rotate-90'}`}
                                    aria-hidden="true"
                                />
                                <span className="text-lg font-semibold text-default">{name}</span>
                            </span>
                        </button>
                        {/* One line. The card is sized so a normal path fits; only a
                            pathological one is clipped, with the full path on hover. */}
                        {system.hierarchy_path && (
                            <div
                                className="pl-[22px] text-xs text-muted font-mono whitespace-nowrap overflow-hidden text-ellipsis"
                                title={system.hierarchy_path}
                                data-testid={`card-path-${system.id}`}
                            >
                                {system.hierarchy_path}
                            </div>
                        )}
                    </div>
                    <span className="shrink-0 flex items-center gap-2">
                        <span
                            className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border font-mono text-[11px] font-semibold uppercase tracking-wider ${TONE_PILL[tone]}`}
                            title={stripeTitle(system)}
                            data-testid={`card-summary-${system.id}`}
                        >
                            <span className={`w-1.5 h-1.5 rounded-full ${TONE_DOT[tone]}`} />
                            {TONE_SUMMARY[tone]}
                        </span>
                        <button
                            type="button"
                            onClick={(e) => {
                                e.stopPropagation();
                                onOpen();
                            }}
                            aria-label={`Open ${name}`}
                            title="Open system"
                            data-testid={`card-open-${system.id}`}
                            className="p-1 rounded text-muted hover:text-primary-500 hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500"
                        >
                            <ArrowUpRight className="w-4 h-4" />
                        </button>
                    </span>
                </div>

                {expanded && (
                <div id={bodyId} className="space-y-4" data-testid={bodyId}>
                {/* Governed execution path */}
                <div className="space-y-2">
                    <div className="flex items-center justify-between gap-2">
                        <span className={MONO_LABEL}>Governed execution path</span>
                        {system.role && (
                            <span className={MONO_LABEL} data-testid={`card-role-${system.id}`}>
                                {AI_SYSTEM_ROLE_LABELS[system.role]}
                            </span>
                        )}
                    </div>
                    <div className="flex items-stretch gap-1.5">
                        <PathNode
                            icon={kindIcon}
                            title={kind}
                            testId={`card-node-agent-${system.id}`}
                            lines={agentLines(facts)}
                        />
                        <ArrowRight className="w-3.5 h-3.5 text-muted self-center shrink-0" />
                        <PathNode
                            icon={<ShieldCheck className="w-3.5 h-3.5 shrink-0" />}
                            title="Gateway"
                            subtitle="enforce"
                            highlight={tone}
                            testId={`card-node-gateway-${system.id}`}
                            lines={gatewayLines(facts, system.enforce_accepted_callers)}
                        />
                        <ArrowRight className="w-3.5 h-3.5 text-muted self-center shrink-0" />
                        <PathNode
                            icon={<Boxes className="w-3.5 h-3.5 shrink-0" />}
                            title="Resources"
                            testId={`card-node-resources-${system.id}`}
                            lines={[
                                `${res.models} model${res.models === 1 ? '' : 's'} · ${res.mcp_servers} MCP`,
                                `${res.skills} skill${res.skills === 1 ? '' : 's'}${
                                    res.delegate_agents != null
                                        ? ` · ${res.delegate_agents} delegate${res.delegate_agents === 1 ? '' : 's'}`
                                        : ''
                                }`,
                            ]}
                        />
                    </div>
                    {(c.depends_on > 0 || c.used_by_declared > 0 || c.used_by_observed > 0) && (
                        <div className="text-xs text-muted flex flex-wrap items-center gap-x-3 gap-y-1">
                            {c.depends_on > 0 && (
                                <span>
                                    depends on {c.depends_on}
                                    {c.worst_dependency_health && (
                                        <>
                                            {c.depends_on === 1 ? ', which is ' : ', the worst '}
                                            <Badge className={DEPENDENCY_HEALTH_CLASSES[c.worst_dependency_health]}>
                                                {DEPENDENCY_HEALTH_LABELS[c.worst_dependency_health]}
                                            </Badge>
                                        </>
                                    )}
                                </span>
                            )}
                            {(c.used_by_declared > 0 || c.used_by_observed > 0) && (
                                <span>
                                    used by {c.used_by_declared} declared / {c.used_by_observed} observed
                                </span>
                            )}
                        </div>
                    )}
                </div>

                {/* The three judgments */}
                <div className="grid grid-cols-3 rounded-lg border border-default bg-surface-subtle divide-x divide-default">
                    <Stat
                        label="Risk"
                        info={CARD_INFO.risk}
                        value={effectiveTier ?? 'not set'}
                        valueClass={
                            effectiveTier === 'high' || effectiveTier === 'unacceptable'
                                ? 'text-red-600 dark:text-red-400'
                                : effectiveTier === 'limited'
                                    ? 'text-amber-600 dark:text-amber-400'
                                    : effectiveTier
                                        ? 'text-green-700 dark:text-green-400'
                                        : 'text-muted'
                        }
                        note={tierLifted ? `declared ${system.eu_ai_act_risk_tier ?? 'not set'}` : undefined}
                    />
                    <Stat
                        label="Health"
                        info={CARD_INFO.health}
                        value={health?.label ?? 'Not judged'}
                        valueClass={health ? TONE_TEXT[assuranceTone(system.health_state, 'assured')] : 'text-muted'}
                    />
                    <Stat
                        label="Verdict"
                        info={CARD_INFO.verdict}
                        value={verdict?.label ?? 'No verdict'}
                        valueClass={verdict ? TONE_TEXT[assuranceTone('healthy', system.verdict_status)] : 'text-muted'}
                        testId={`card-verdict-${system.id}`}
                    />
                </div>

                {facts && <ObservabilityPanel systemId={system.id} kpis={facts.kpis} />}

                {/* Governance documentation */}
                <div
                    className={`flex items-start gap-3 rounded-lg border p-3 ${
                        documented
                            ? 'border-green-500/40 bg-green-500/10'
                            : 'border-amber-500/40 bg-amber-500/10'
                    }`}
                    data-testid={`card-governance-${system.id}`}
                >
                    <span
                        className={`shrink-0 w-7 h-7 rounded-md flex items-center justify-center ${
                            documented ? 'bg-green-500/20 text-green-700 dark:text-green-300' : 'bg-amber-500/20 text-amber-700 dark:text-amber-300'
                        }`}
                    >
                        {documented ? <Check className="w-4 h-4" /> : <AlertTriangle className="w-4 h-4" />}
                    </span>
                    <div className="min-w-0">
                        <div className="text-sm font-medium text-default">
                            {documented
                                ? 'Governance documented'
                                : `${gaps.length + (findingCount > 0 ? 1 : 0)} governance gap${gaps.length + (findingCount > 0 ? 1 : 0) === 1 ? '' : 's'}`}
                        </div>
                        <div className="text-xs text-muted mt-0.5">
                            {documented
                                ? 'Owner, intended use, lifecycle stage and risk tier are on file.'
                                : [
                                    ...gaps.map((g) => GOVERNANCE_GAP_LABELS[g] ?? g),
                                    ...(findingCount > 0
                                        ? [`${findingCount} composition finding${findingCount === 1 ? '' : 's'}`]
                                        : []),
                                ].join(' · ')}
                        </div>
                    </div>
                </div>
                </div>
                )}
            </div>

            {/* Footer — owner and liveness stay visible when collapsed. */}
            <div className="flex items-center justify-between gap-3 px-5 py-3 border-t border-default">
                <span className="min-w-0 truncate">
                    <span className={MONO_LABEL}>Owner </span>
                    <span className="text-xs text-secondary">{system.owner || 'not set'}</span>
                </span>
                <span className="flex items-center gap-2 shrink-0">
                    {system.retirable === true && (
                        <Badge className="bg-amber-100 text-amber-800">Ready to retire</Badge>
                    )}
                    {system.liveness && (
                        <LivenessBadge
                            state={system.liveness.state}
                            reason={system.liveness.reason}
                            testId={`card-liveness-${system.id}`}
                        />
                    )}
                    {expanded && (
                        <button
                            type="button"
                            onClick={onOpen}
                            className="text-xs text-primary-600 dark:text-primary-400 hover:underline inline-flex items-center gap-1"
                        >
                            Open system <ArrowRight className="w-3 h-3" />
                        </button>
                    )}
                </span>
            </div>
        </div>
    );
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`;

/** Agent small print: implementation + version, identity, outcomes, contract.
 *  Anything not known is left out rather than shown as a placeholder. */
function agentLines(facts: AISystemCardFacts | null): string[] {
    if (!facts) return [];
    const a = facts.agent;
    const lines: string[] = [];
    if (a.impl_name || a.impl_version) {
        lines.push(
            [a.impl_name, a.impl_version ? `v${a.impl_version}` : null].filter(Boolean).join(' ')
            + (a.impl_trust === 'verified' ? ' ✓' : ''),
        );
    } else {
        lines.push('version not reported');
    }
    if (a.identity_id) {
        lines.push(`ID ${a.identity_id}${a.identity_count > 1 ? ` +${a.identity_count - 1}` : ''}`);
    }
    lines.push(plural(a.declared_outcomes, 'outcome'));
    if (a.contract_version != null) {
        lines.push(`contract v${a.contract_version} · ${a.contract_status ?? 'unknown'}`);
    }
    return lines;
}

function gatewayLines(facts: AISystemCardFacts | null, listedCallersOnly?: boolean): string[] {
    if (!facts) return listedCallersOnly ? ['listed callers only'] : [];
    const g = facts.gateway;
    const lines = [
        `${plural(g.policies, 'policy').replace('policys', 'policies')} · ${plural(g.limits, 'limit')}`,
        plural(g.guardrails, 'guardrail'),
        `${plural(g.blocks, 'block')} (${windowLabel(facts.kpis.window_hours)})`,
    ];
    if (listedCallersOnly) lines.push('listed callers only');
    return lines;
}

function windowLabel(hours: number): string {
    return hours % 24 === 0 ? `${hours / 24}d` : `${hours}h`;
}

function fmtMs(ms: number | null): string {
    if (ms == null) return '—';
    return ms >= 1000 ? `${(ms / 1000).toFixed(ms >= 10_000 ? 0 : 1)}s` : `${ms}ms`;
}

function fmtCompact(n: number): string {
    return n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1_000 ? `${(n / 1_000).toFixed(1)}k` : String(n);
}

function fmtUsd(v: number): string {
    return v === 0 ? '$0' : v < 0.01 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`;
}

/** A per-run average: cents round most agent runs to "$0.02", so keep a
 *  third significant decimal below a dollar. */
function fmtUsdPerRun(v: number): string {
    return v === 0 ? '$0' : v < 0.001 ? `$${v.toFixed(4)}` : v < 1 ? `$${v.toFixed(3)}` : `$${v.toFixed(2)}`;
}

const pct = (v: number | null) => (v == null ? '—' : `${Math.round(v * 100)}%`);

/**
 * Observability & evaluation: tracing (runs = traces, actions = spans),
 * the KPIs (latency, cost, input/output tokens, success and failures),
 * and offline evaluation (replay suites, latest pass rate) next to the
 * live signals — a demo that worked once is not an eval.
 */
function ObservabilityPanel({ systemId, kpis }: { systemId: string; kpis: AISystemCardKpis }) {
    const noRuns = kpis.traces === 0;
    // A shared service roots no runs: its figures are the calls it served
    // inside other systems' runs. Say so — "Traces 105"
    // would claim runs this system never started.
    const served = kpis.basis === 'served_calls';
    const unit = served ? 'call' : 'trace';
    return (
        <div
            className="rounded-lg border border-default"
            data-testid={`card-observability-${systemId}`}
            data-basis={kpis.basis}
        >
            <div className="flex items-center justify-between px-3 pt-2.5">
                <span className={MONO_LABEL}>Observability &amp; evaluation</span>
                <span className={MONO_LABEL}>last {windowLabel(kpis.window_hours)}</span>
            </div>
            {served && (
                <p className="px-3 pt-1 text-[11px] text-muted" data-testid={`card-served-basis-${systemId}`}>
                    Shared service: figures are the calls it served inside other systems’ runs.
                </p>
            )}
            <div className="grid grid-cols-3 gap-x-3 gap-y-2.5 px-3 py-2.5">
                <Kpi
                    label={served ? 'Served calls' : 'Traces'}
                    info={served ? CARD_INFO.servedCalls : CARD_INFO.traces}
                    value={fmtCompact(kpis.traces)}
                    note={`${fmtCompact(kpis.spans)} spans`}
                    testId={`card-traces-${systemId}`}
                />
                <Kpi
                    label="Success"
                    info={served ? CARD_INFO.servedSuccess : CARD_INFO.success}
                    value={pct(kpis.success_rate)}
                    note={noRuns ? 'no runs' : `${plural(kpis.failed, 'failure')}`}
                    valueClass={
                        kpis.success_rate == null
                            ? 'text-muted'
                            : kpis.success_rate >= 0.95
                                ? 'text-green-700 dark:text-green-400'
                                : kpis.success_rate >= 0.8
                                    ? 'text-yellow-700 dark:text-yellow-300'
                                    : 'text-red-600 dark:text-red-400'
                    }
                />
                <Kpi
                    label="Latency p50"
                    info={served ? CARD_INFO.servedLatency : CARD_INFO.latency}
                    value={fmtMs(kpis.p50_latency_ms)}
                    note={`p95 ${fmtMs(kpis.p95_latency_ms)}`}
                    testId={`card-latency-${systemId}`}
                />
                <Kpi
                    label="Cost"
                    info={CARD_INFO.cost}
                    value={fmtUsd(kpis.cost_usd)}
                    note={noRuns ? 'no runs' : `${fmtUsdPerRun(kpis.cost_usd / kpis.traces)} / ${unit}`}
                    testId={`card-cost-${systemId}`}
                />
                <Kpi
                    label="Tokens"
                    info={CARD_INFO.tokens}
                    value={fmtCompact(kpis.tokens)}
                    note={`in ${fmtCompact(kpis.input_tokens)} · out ${fmtCompact(kpis.output_tokens)}`}
                    testId={`card-tokens-${systemId}`}
                />
                <Kpi
                    label="Offline evals"
                    info={CARD_INFO.evals}
                    value={kpis.last_eval_pass_rate != null ? pct(kpis.last_eval_pass_rate) : kpis.eval_suites ? 'not run' : 'none'}
                    note={
                        kpis.eval_suites
                            ? `${plural(kpis.eval_suites, 'suite')}${
                                kpis.last_eval_at ? ` · ${new Date(kpis.last_eval_at).toLocaleDateString()}` : ''
                            }`
                            : 'no replay suite'
                    }
                    valueClass={kpis.eval_suites ? 'text-default' : 'text-amber-700 dark:text-amber-300'}
                    testId={`card-evals-${systemId}`}
                />
            </div>
        </div>
    );
}

/** The card's (i) marker: the shared click-to-open HelpTip, sized for a
 *  9–10px mono label. Test ids are `card-info-{label}` (lower-kebab). */
function InfoMark({ label, text }: { label: string; text: string }) {
    const slug = label.toLowerCase().replace(/[^a-z0-9]+/g, '-');
    return (
        <HelpTip
            title={label}
            text={text}
            ariaLabel={`What does ${label} mean?`}
            iconClassName="w-3 h-3"
            testId={`card-info-${slug}`}
            className="normal-case tracking-normal"
        />
    );
}

/**
 * What each card figure means. Kept next to the backend's definitions
 * (`ai_system_card_facts.py`, `ai_system_outcomes.py`,
 * `ai_system_assurance._verdict`, `ai_system_health.py`) — change one,
 * change both.
 */
const CARD_INFO = {
    traces:
        'Runs of this system that started in the window — one run is one ' +
        'task end to end: every model call, MCP tool call, skill run and ' +
        'call to another agent made for that task shares one task id and ' +
        'forms one trace. Spans (below) is the total number of those ' +
        'governed calls, so spans ÷ traces is roughly the steps a typical ' +
        'task takes. Offline-eval replays are never counted.',
    servedCalls:
        'This system started no runs of its own in the window — it is a ' +
        'shared service, called (over A2A or as a delegate) inside other ' +
        'systems’ runs. Each served call is its slice of one caller run; ' +
        'every figure on this panel is computed over those calls only, not ' +
        'the caller runs’ totals. Spans (below) is the governed calls the ' +
        'service itself made. The Runs tab lists them.',
    servedSuccess:
        'Share of served calls the service answered with an outcome its ' +
        'own contract declares. Failed or timed-out calls count against ' +
        'it, and so does an answer with an undeclared outcome (a ' +
        'deviation); a call the gateway refused never reached the service ' +
        'and is left out. Same classification its Health uses. Green ≥ ' +
        '95%, amber ≥ 80%.',
    servedLatency:
        'Time the service spent on each served call: the summed duration ' +
        'of its own model and tool calls in that run. p50 is the median ' +
        'call; p95 means 95% of calls took no longer. Nearest-rank values ' +
        '— a duration actually observed.',
    success:
        'Share of decisive runs that completed with an outcome the system ' +
        'declared in its outcome catalogue. Decisive = succeeded, failed, ' +
        'or completed with an undeclared outcome (a deviation); abandoned ' +
        'or cancelled runs and declared “nothing to do” runs are left out. ' +
        'Failures (below) counts runs that ended without completing ' +
        '(errored, timed out or were refused). Green ≥ 95%, ' +
        'amber ≥ 80%.',
    latency:
        'Run duration, start to finish, over the runs in the window that ' +
        'report one. p50 is the median run; p95 means 95% of runs finished ' +
        'within that time. Both are nearest-rank values — a duration that ' +
        'was actually observed, not an interpolation.',
    cost:
        'Total spend of the runs in the window, priced by the gateway from ' +
        'each model’s configured token prices, and the average per trace ' +
        '(per run) — per served call for a shared service. Replays are not ' +
        'included.',
    tokens:
        'Total tokens the runs in the window used, split into input ' +
        '(prompt: instructions, conversation, tool results and retrieved ' +
        'context sent to the model) and output (what the model generated). ' +
        'Output tokens are usually priced several times higher than input. ' +
        'Replays are not included.',
    evals:
        'Offline evaluation: replay suites re-run recorded tasks against ' +
        'the system and score the answers. Shows the pass rate of the most ' +
        'recent completed replay and how many suites are enabled. “Not ' +
        'run” = a suite exists but has never finished; “none” = no replay ' +
        'suite, so nothing tests the system outside live traffic.',
    risk:
        'EU AI Act risk tier: minimal, limited, high or unacceptable. Shows ' +
        'the effective tier — raised to the tier of a riskier system that ' +
        'relies on this one for its decision, with “declared …” noting the ' +
        'system’s own tier. High adds a human-approval floor to every ' +
        'lifecycle transition and selects the obligations on the EU AI Act ' +
        'board. It does not block runtime calls by itself.',
    health:
        'How the system is running right now, over a fixed 30-day period ' +
        'plus its latest runs. Worst-of its signals — liveness, behaviour, ' +
        'reliability, cost, conformance, oversight and review debt — never ' +
        'an average. Healthy, Learning (baselines still forming), Degraded, ' +
        'Drifting (open behaviour finding), Stalled, Failing (latest runs ' +
        'all errored), Silent (stopped calling), Suspended, or Not judged ' +
        '(too little data).',
    verdict:
        'The assurance verdict for the 30-day period: did the system stay ' +
        'within its approved operating envelope? Assured; Assured with ' +
        'exceptions (within it, but with governed refusals, open drift, ' +
        'unevaluable terms or an unhealthy critical dependency on record); ' +
        'Violations (an envelope term was breached); Insufficient evidence ' +
        '(too few runs to judge — a quiet period is not a clean one).',
} as const;

function Kpi({
    label,
    value,
    note,
    info,
    valueClass = 'text-default',
    testId,
}: {
    label: string;
    value: string;
    note?: string;
    /** What the figure means — behind an (i) next to the label. */
    info: string;
    valueClass?: string;
    testId?: string;
}) {
    return (
        <div className="min-w-0" data-testid={testId}>
            <div className="flex items-center gap-1 font-mono text-[9px] uppercase tracking-widest text-muted">
                {label}
                <InfoMark label={label} text={info} />
            </div>
            <div className={`text-sm font-semibold tabular-nums ${valueClass}`}>{value}</div>
            {note && <div className="text-[10px] text-muted leading-tight">{note}</div>}
        </div>
    );
}

function PathNode({
    icon,
    title,
    subtitle,
    lines,
    highlight,
    testId,
}: {
    icon: React.ReactNode;
    title: string;
    subtitle?: string;
    /** Small print under the title, one fact per line. */
    lines: string[];
    /** Set on the gateway node: it takes the card's tone. */
    highlight?: AssuranceTone;
    testId?: string;
}) {
    return (
        <div
            data-testid={testId}
            className={`flex-1 min-w-0 rounded-lg border px-2 py-2 ${
                highlight ? `border-2 ${TONE_PILL[highlight]}` : 'border-default bg-surface-subtle text-secondary'
            }`}
        >
            {/* Icon on the title line — an icon row of its own would cost a line. */}
            <div className="flex items-center gap-1.5 text-default">
                {icon}
                <span className="text-[11px] font-semibold uppercase tracking-wider">{title}</span>
                {subtitle && <span className="font-mono text-[10px] text-muted">({subtitle})</span>}
            </div>
            {lines.length > 0 && (
                <div className="mt-1 space-y-0.5">
                    {lines.map((line) => (
                        <div key={line} className="font-mono text-[10px] leading-tight text-muted break-words">
                            {line}
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}

function Stat({
    label,
    value,
    valueClass,
    note,
    info,
    testId,
}: {
    label: string;
    value: string;
    valueClass: string;
    note?: string;
    /** What the field means — behind an (i) next to the label. */
    info: string;
    testId?: string;
}) {
    // Smaller and wrapping, never truncated: "Assured w/ exceptions" must
    // read in full — a cut-off verdict is a different verdict.
    return (
        <div className="px-3 py-2.5 min-w-0" data-testid={testId}>
            <div className={`flex items-center gap-1 ${MONO_LABEL}`}>
                {label}
                <InfoMark label={label} text={info} />
            </div>
            <div className={`mt-1 text-xs font-bold uppercase leading-snug break-words ${valueClass}`}>
                {value}
            </div>
            {note && <div className="text-[10px] text-muted">{note}</div>}
        </div>
    );
}

/** Operator-facing names for the stripe, surfaced as a tooltip — a colour
 *  without a word is a guess. */
function stripeTitle(system: AISystemSummary): string {
    const health = system.health_state ?? 'not yet judged';
    const verdict = system.verdict_status
        ? system.verdict_status.replace(/_/g, ' ')
        : 'no verdict yet';
    return `Health: ${health} · Verdict: ${verdict}`;
}

function AISystemRow({
    system,
    onOpen,
}: {
    system: AISystemSummary;
    onOpen: () => void;
}) {
    const Icon = resolveGroupIcon(undefined, GroupType.AI_SYSTEM, system.system_kind);
    const findingCount = system.composition_finding_count ?? 0;

    return (
        <tr
            className="hover:bg-surface-hover cursor-pointer"
            data-testid={`ai-system-row-${system.id}`}
            onClick={onOpen}
        >
            <td className="py-3 pr-4 pl-0">
                <div className="flex items-center gap-3">
                    {/* The assurance stripe — same ladder as the Mission
                        Control cards, so the two surfaces cannot disagree
                        about which systems need attention. */}
                    <span
                        className={`w-1.5 self-stretch min-h-10 rounded-r ${assuranceStripe(system.health_state, system.verdict_status)}`}
                        title={stripeTitle(system)}
                        data-testid={`stripe-${system.id}`}
                        aria-hidden="true"
                    />
                    <Icon className="w-5 h-5 text-secondary shrink-0" />
                    <span className="min-w-0">
                        <span className="block text-sm font-medium text-default truncate">
                            {system.display_name || system.name}
                        </span>
                        {system.hierarchy_path && (
                            <span className="block text-xs text-muted truncate">
                                {system.hierarchy_path}
                            </span>
                        )}
                    </span>
                </div>
            </td>
            <td className="px-4 py-3 text-sm text-secondary">
                {systemKindLabel(system.system_kind) || <Unset />}
            </td>
            <td className="px-4 py-3 text-sm text-secondary">
                {system.owner || <Unset />}
            </td>
            <td className="px-4 py-3">
                {/* A deprecated system that has gone quiet is the only row
                    here that needs an action and never asks for one. Without
                    this badge nobody re-opens the page that said "not yet",
                    and deprecated systems accumulate indefinitely. */}
                {system.retirable === true && (
                    <Badge
                        className="mr-1.5 bg-amber-100 text-amber-800"
                        testId={`retirable-${system.id}`}
                    >
                        Ready to retire
                    </Badge>
                )}
                {system.lifecycle_stage ? (
                    <Badge className={LIFECYCLE_CLASSES[system.lifecycle_stage] ?? 'bg-slate-100 text-slate-700'}>
                        {LIFECYCLE_STAGE_LABELS[system.lifecycle_stage] ??
                            system.lifecycle_stage}
                    </Badge>
                ) : (
                    <Unset />
                )}
            </td>
            <td className="px-4 py-3">
                <RiskCell system={system} />
            </td>
            <td className="px-4 py-3 text-xs text-muted">
                <CompositionCell system={system} />
            </td>
            <td className="px-4 py-3">
                {system.governance_gaps.length === 0 && findingCount === 0 ? (
                    <Badge className="bg-emerald-100 text-emerald-800">Documented</Badge>
                ) : (
                    <span className="flex flex-wrap gap-1" data-testid={`gaps-${system.id}`}>
                        {system.governance_gaps.map((gap) => (
                            <Badge key={gap} className="bg-amber-100 text-amber-800">
                                {GOVERNANCE_GAP_LABELS[gap] ?? gap}
                            </Badge>
                        ))}
                        {/* Declarations that disagree with
                            what the gateway observed are a governance gap too. */}
                        {findingCount > 0 && (
                            <Badge className="bg-amber-100 text-amber-800" testId={`composition-findings-${system.id}`}>
                                {findingCount} composition finding{findingCount === 1 ? '' : 's'}
                            </Badge>
                        )}
                    </span>
                )}
            </td>
        </tr>
    );
}

/**
 * Declared tier, and — where a contributing caller lifts it — the effective
 * tier with its source ("minimal → high via Brutor Demo System"). A declared
 * separability rationale shows as a marker.
 */
function RiskCell({ system }: { system: AISystemSummary }) {
    const declared = system.eu_ai_act_risk_tier ?? null;
    const effective = system.risk_tier_effective ?? null;
    const lifted = Boolean(effective && effective !== declared);
    if (!declared && !effective) return <Unset />;
    return (
        <span className="flex flex-col items-start gap-1" data-testid={`risk-${system.id}`}>
            {declared ? (
                <Badge className={RISK_TIER_CLASSES[declared] ?? 'bg-slate-100 text-slate-700'}>{declared}</Badge>
            ) : (
                <Unset />
            )}
            {lifted && effective && (
                <span className="text-xs text-secondary" data-testid={`risk-effective-${system.id}`}>
                    effective{' '}
                    <Badge className={RISK_TIER_CLASSES[effective] ?? 'bg-slate-100 text-slate-700'}>{effective}</Badge>
                    <span className="block text-muted">
                        {effectiveTierText(declared, effective, system.effective_tier_sources)}
                    </span>
                </span>
            )}
            {system.separability_declared && (
                <span
                    className={`text-xs ${system.separability_sealed ? 'text-muted' : 'text-amber-800'}`}
                    data-testid={`risk-separability-${system.id}`}
                    data-sealed={system.separability_sealed ? 'true' : 'false'}
                    title={
                        system.separability_sealed
                            ? 'A dependency on this path is declared as not contributing to the decision; every such claim is sealed as a declaration record'
                            : 'A separability claim on this path is not sealed yet — the compliance registry does not accept it until it is (save the dependency again to retry)'
                    }
                >
                    separability declared · {system.separability_sealed ? 'sealed' : 'not sealed'}
                </span>
            )}
        </span>
    );
}

/**
 * The system's place in the composition: member counts,
 * "depends on N" with the worst dependency health, "used by M" (declared
 * vs observed callers) and the liveness verdict. A shared service nobody
 * called in the window reads "Idle" — never an empty cell.
 */
function CompositionCell({ system }: { system: AISystemSummary }) {
    const c = system.composition;
    const m = c.members;
    return (
        <span className="flex flex-col gap-1" data-testid={`composition-${system.id}`}>
            <span className="whitespace-nowrap">
                {m.agents} agents · {m.models} models · {m.mcp_servers} MCP · {m.skills} skills
            </span>
            <span className="flex flex-wrap items-center gap-1.5">
                {system.role && (
                    <Badge className="bg-surface-raised text-secondary border border-default" testId={`role-${system.id}`}>
                        {AI_SYSTEM_ROLE_LABELS[system.role]}
                    </Badge>
                )}
                {c.depends_on > 0 && (
                    <span data-testid={`depends-on-${system.id}`}>
                        depends on {c.depends_on}
                        {/* The DEPENDENCY's health, not this system's: say so,
                            or a green badge in this row reads as the system's
                            own verdict. */}
                        {c.worst_dependency_health && (
                            <>
                                {c.depends_on === 1 ? ', which is ' : ', the worst '}
                                <Badge className={DEPENDENCY_HEALTH_CLASSES[c.worst_dependency_health]}>
                                    {DEPENDENCY_HEALTH_LABELS[c.worst_dependency_health]}
                                </Badge>
                            </>
                        )}
                    </span>
                )}
                {system.has_external_dependencies && (
                    <span className="text-muted" data-testid={`external-${system.id}`}>· external</span>
                )}
                {system.enforce_accepted_callers && (
                    <Badge
                        className="bg-surface-raised text-secondary border border-default"
                        testId={`enforced-callers-${system.id}`}
                    >
                        listed callers only
                    </Badge>
                )}
                {/* Liveness is the control plane's ONE verdict through the shared mapping — a service nobody called
                    reads "Idle" because the liveness function says so, not
                    because this cell inferred it. */}
                {system.liveness && (
                    <LivenessBadge
                        state={system.liveness.state}
                        reason={system.liveness.reason}
                        testId={`liveness-${system.id}`}
                    />
                )}
                {(c.used_by_declared > 0 || c.used_by_observed > 0) && (
                    <span data-testid={`used-by-${system.id}`}>
                        used by {c.used_by_declared} declared / {c.used_by_observed} observed
                    </span>
                )}
            </span>
        </span>
    );
}

/**
 * Column explainers — what the field means, and crucially whether it is
 * descriptive metadata or something the platform's machinery acts on.
 * Every "operational" claim below is backed by a real consumer:
 * lifecycle → ai_system_lifecycle_gate, risk → ai_obligations +
 * lifecycle-gate floor + derived component risk, composition → the
 * resource bindings that ARE runtime access control.
 */
interface HeaderInfo {
    /** What the column shows. */
    what: string;
    /** Does its value change how the platform behaves? */
    operational: boolean;
    /** The impact sentence — what acts on it, or why it is info-only. */
    impact: string;
}

const HEADER_INFO: Record<string, HeaderInfo> = {
    kind: {
        what:
            'Classification of what this AI System is — Agent, Assistant, ' +
            'Integration, and so on. Drives the icon and the Kind filter ' +
            'on this board.',
        operational: false,
        impact:
            'Descriptive only. Kind never changes enforcement or routing — ' +
            'what the system may call is governed by its resource bindings, ' +
            'not by how it is classified.',
    },
    owner: {
        what: 'The person or team accountable for this system.',
        operational: false,
        impact:
            'Informational, but tracked: a missing owner is a governance ' +
            'gap — it shows as a chip in the Governance column, keeps the ' +
            'system undocumented, and appears in assurance reports. It does ' +
            'not gate traffic.',
    },
    lifecycle: {
        what:
            'Where the system is in its life: proposed → approved → active ' +
            '→ deprecated → retired.',
        operational: true,
        impact:
            'Stage transitions are gated: each move has evidence ' +
            'requirements (tenant-configurable), and high-risk systems ' +
            'require human approval on every transition. The stage itself ' +
            'does not stop runtime traffic — it is what assurance holds ' +
            'the system to.',
    },
    risk: {
        what:
            'The EU AI Act risk tier declared for this system: minimal, ' +
            'limited, high, or unacceptable — and, where a system that ' +
            'relies on it for its decision is riskier, the effective tier ' +
            'with the system it comes from. A separability claim shows ' +
            'whether it is sealed as a declaration record.',
        operational: true,
        impact:
            'Selects which obligations apply on the EU AI Act board (at ' +
            'the effective tier), adds ' +
            'a non-removable human-approval floor to lifecycle transitions ' +
            'when high, and components inherit the worst tier of the ' +
            'systems that use them in the Asset Register. It does not ' +
            'block runtime calls by itself.',
    },
    composition: {
        what:
            'What the system is built from — its member agents, and the ' +
            'models, MCP servers and skills it can reach (its own plus ' +
            'those inherited from parent groups; the gallery card shows ' +
            'the same counts) — and its place among other ' +
            'systems: how many it depends on (with the worst dependency ' +
            'health) and how many call it, declared vs observed. A shared ' +
            'service nobody called in the window shows “Idle” — the ' +
            'liveness verdict (alive, silent, idle, unstarted, not ' +
            'configured) is the same one the system’s Liveness card shows.',
        operational: true,
        impact:
            'The bindings behind the counts ARE the system’s runtime ' +
            'access control — they decide what it may call. Dependency ' +
            'declarations do not gate traffic; a mismatch with what was ' +
            'observed is a composition finding — except on a service ' +
            'marked “listed callers only”, where the gateway refuses any ' +
            'calling system it does not accept. Open the system’s ' +
            'Composition tab for the full picture.',
    },
    governance: {
        what:
            'Documentation completeness: “Documented”, or chips naming ' +
            'each missing declaration (owner, intended use, lifecycle ' +
            'stage, risk tier) and the number of composition findings ' +
            '(undeclared dependencies, unaccepted callers, …).',
        operational: false,
        impact:
            'Informational with downstream effect: gaps keep EU AI Act ' +
            'applicability “undetermined” and assurance never reports ' +
            'green on absent evidence. It is a completeness signal, not a ' +
            'compliance verdict.',
    },
};

function Th({ children, info }: { children: string; info?: HeaderInfo }) {
    const [open, setOpen] = useState(false);
    const ref = useRef<HTMLButtonElement>(null);
    const close = useCallback(() => setOpen(false), []);

    if (!info) {
        return (
            <th className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                {children}
            </th>
        );
    }

    // The panel is portalled by AnchoredPopover: rendered inside the <th> it
    // would be clipped by the table's scroll wrapper whenever the list has
    // only a few rows.
    return (
        <th className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
            <button
                ref={ref}
                type="button"
                onClick={() => setOpen((v) => !v)}
                aria-expanded={open}
                data-testid={`th-info-${children.toLowerCase()}`}
                className="inline-flex items-center gap-1 uppercase tracking-wider hover:text-secondary transition-colors"
                title={`What does ${children} mean?`}
            >
                {children}
                <HelpCircle className="w-3 h-3 opacity-60" />
            </button>
            <AnchoredPopover
                anchorRef={ref}
                open={open}
                onClose={close}
                testId={`th-info-popover-${children.toLowerCase()}`}
                className="space-y-2"
            >
                <div className="text-xs font-semibold text-default">{children}</div>
                <p className="text-xs text-secondary font-normal leading-relaxed">{info.what}</p>
                <div>
                    <span
                        className={`inline-flex items-center px-2 py-0.5 rounded text-[11px] font-medium ${
                            info.operational
                                ? 'bg-blue-100 text-blue-800'
                                : 'bg-slate-100 text-slate-700'
                        }`}
                    >
                        {info.operational ? 'Operational impact' : 'Informational'}
                    </span>
                </div>
                <p className="text-xs text-secondary font-normal leading-relaxed">{info.impact}</p>
            </AnchoredPopover>
        </th>
    );
}

function Badge({
    children,
    className,
    testId,
}: {
    children: React.ReactNode;
    className: string;
    /** Opt-in hook for tests. Without it the component swallows the
     *  attribute, and a badge that renders is indistinguishable from one
     *  that does not. */
    testId?: string;
}) {
    return (
        <span
            className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${className}`}
            data-testid={testId}
        >
            {children}
        </span>
    );
}

/** A governance attribute nobody has filled in — shown, never faked. */
function Unset() {
    return <span className="text-xs text-muted italic">not set</span>;
}

function EmptyState({ filtered }: { filtered: boolean }) {
    return (
        <div className="p-10 text-center border border-dashed border-strong rounded-lg">
            <ShieldQuestion className="w-8 h-8 text-muted mx-auto mb-3" />
            {filtered ? (
                <p className="text-sm text-muted">
                    No AI Systems match these filters.
                </p>
            ) : (
                <>
                    <p className="text-sm text-default font-medium">No AI Systems yet</p>
                    <p className="text-sm text-muted mt-1 max-w-lg mx-auto">
                        Create a resource group with type <strong>AI System</strong> to
                        register a deployed agent, assistant, application or integration.
                        Everything the platform governs and monitors hangs off that entity.
                    </p>
                </>
            )}
        </div>
    );
}

export default AISystemsFleetTab;

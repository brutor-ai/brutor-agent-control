/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Mission Control for A2A delegation graphs.
 *
 * Composition:
 *   - Stats header (KPIs: total graphs, step counts, success rate, p50/p95)
 *   - Filters: success (all/ok/fail), time window (since)
 *   - Paginated list of graphs (one row per root_task_id)
 *   - Drill into detail on click (renders <A2ADelegationGraphDetail />)
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
    Check,
    Clock,
    GitBranch,
    Loader2,
    RefreshCw,
    TrendingUp,
    XCircle,
} from 'lucide-react';

import { useAuth } from '../../hooks/useAuth';
import InfoBanner from '../common/InfoBanner';
import type {
    A2ADelegationGraphSummary,
    A2ADelegationStats,
} from '../../lib/a2aTypes';
import {
    formatDate,
    formatDurationMs,
    formatInt,
    formatPercent,
    formatRelative,
} from './a2aFormatters';
import A2ADelegationGraphDetail from './A2ADelegationGraphDetail';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';

type Filter = 'all' | 'success' | 'failure';
type Window = 'hour' | 'day' | 'week' | 'all';

function sincePreset(preset: Window): string | undefined {
    if (preset === 'all') return undefined;
    const now = Date.now();
    const delta =
        preset === 'hour'
            ? 60 * 60 * 1000
            : preset === 'day'
              ? 24 * 60 * 60 * 1000
              : 7 * 24 * 60 * 60 * 1000;
    return new Date(now - delta).toISOString();
}

interface A2ADelegationGraphsTabProps {
    /** Opens another page (App tab id) — each row's AI Systems link to
     *  their Composition tab. */
    setActiveTab?: (tab: string) => void;
}

const A2ADelegationGraphsTab: React.FC<A2ADelegationGraphsTabProps> = ({ setActiveTab }) => {
    const { gatewayClient } = useAuth();

    const [stats, setStats] = useState<A2ADelegationStats | null>(null);
    const [graphs, setGraphs] = useState<A2ADelegationGraphSummary[]>([]);
    const [total, setTotal] = useState(0);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const [filter, setFilter] = useState<Filter>('all');
    const [timeWindow, setTimeWindow] = useState<Window>('day');
    const [limit, setLimit] = useState(50);
    const [offset, setOffset] = useState(0);

    const [activeRoot, setActiveRoot] = useState<string | null>(null);

    const since = useMemo(() => sincePreset(timeWindow), [timeWindow]);

    const load = useCallback(async () => {
        setError(null);
        try {
            const [listResp, statsResp] = await Promise.all([
                gatewayClient.listA2ADelegationGraphs({
                    limit,
                    offset,
                    since,
                    success: filter === 'all' ? undefined : filter === 'success',
                }),
                gatewayClient.getA2ADelegationStats({ since }),
            ]);
            const list = listResp;
            setGraphs(list.graphs);
            setTotal(list.total);
            setStats(statsResp);
        } catch (e) {
            setError((e as Error)?.message ?? 'Failed to load delegation graphs');
        }
    }, [gatewayClient, filter, since, limit, offset]);

    useEffect(() => {
        setLoading(true);
        runBackground(load().finally(() => setLoading(false)), 'load');
    }, [load]);

    async function reload() {
        setRefreshing(true);
        try {
            await load();
        } finally {
            setRefreshing(false);
        }
    }

    useEffect(() => {
        setOffset(0);
    }, [filter, timeWindow, limit]);

    if (activeRoot) {
        return (
            <A2ADelegationGraphDetail
                rootTaskId={activeRoot}
                onBack={() => setActiveRoot(null)}
            />
        );
    }

    const totalSteps = stats?.total_steps ?? 0;

    return (
        <div className="p-6 space-y-6">
            {/* ============ HEADER ============ */}
            <div className="flex items-center justify-between">
                <div className="flex items-center space-x-3">
                    <div
                        className={`p-2.5 rounded-xl ${
                            totalSteps > 0 ? 'bg-primary-100' : 'bg-surface-raised'
                        }`}
                    >
                        <GitBranch
                            className={`w-7 h-7 ${
                                totalSteps > 0 ? 'text-primary-600' : 'text-muted'
                            }`}
                        />
                    </div>
                    <div>
                        <h1 className="text-2xl font-bold text-default">
                            Delegation Graphs
                        </h1>
                        <p className="text-sm text-muted">
                            {stats
                                ? `${stats.total_graphs} graph${stats.total_graphs === 1 ? '' : 's'}, ${stats.total_steps} step${stats.total_steps === 1 ? '' : 's'}`
                                : 'Traces of agent-to-agent calls that ran through this gateway'}
                        </p>
                    </div>
                </div>

                <div className="flex items-center space-x-3">
                    <select
                        value={timeWindow}
                        onChange={e => setTimeWindow(e.target.value as Window)}
                        className="appearance-none pl-3 pr-8 py-2 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 bg-surface text-default"
                        aria-label="Time window"
                    >
                        <option value="hour">Last hour</option>
                        <option value="day">Last 24 hours</option>
                        <option value="week">Last 7 days</option>
                        <option value="all">All time</option>
                    </select>
                    <button
                        onClick={asyncHandler(reload, 'click')}
                        disabled={refreshing}
                        className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors disabled:opacity-50"
                        title="Refresh"
                    >
                        <RefreshCw
                            className={`w-5 h-5 ${refreshing ? 'animate-spin' : ''}`}
                        />
                    </button>
                </div>
            </div>

            {/* ============ INFO BOX ============ */}
            <InfoBanner title="How Delegation Graphs work">
                <p>
                    Every agent-to-agent call produces a <strong>step</strong> row in{' '}
                    <code>proxy_logs</code>. Steps that share the same{' '}
                    <strong>root_task_id</strong> belong to the same graph; the{' '}
                    <strong>parent_task_id</strong> / <strong>current_task_id</strong>{' '}
                    pair on each step lets the view below reconstruct the tree. Use the
                    KPIs to spot anomalies (rising p95 duration, dips in success rate,
                    one card dominating traffic), filter by success/failure to triage,
                    and drill into any root to see every step of that task with its
                    direction (inbound vs outbound), duration, status, and error. The
                    HMAC-signed chain that ties these steps together is unforgeable —
                    a downstream agent can only forward what the gateway signed, not
                    fabricate a shallower graph to escape depth caps.
                </p>
            </InfoBanner>

            {/* ============ KPI ROW ============ */}
            <StatsHeader stats={stats} loading={loading && !stats} />

            {/* ============ FILTER CHIPS ============ */}
            <div className="flex items-center space-x-2">
                <FilterChip
                    active={filter === 'all'}
                    onClick={() => setFilter('all')}
                    label="All"
                />
                <FilterChip
                    active={filter === 'success'}
                    onClick={() => setFilter('success')}
                    label="Success"
                />
                <FilterChip
                    active={filter === 'failure'}
                    onClick={() => setFilter('failure')}
                    label="Failure"
                />
                <div className="ml-auto text-sm text-muted">
                    {loading ? 'Loading…' : `${total} graph${total === 1 ? '' : 's'}`}
                </div>
            </div>

            {error && (
                <InfoBanner variant="error" className="mb-0">
                    <p>{error}</p>
                </InfoBanner>
            )}

            {/* ============ LIST ============ */}
            {loading ? (
                <div className="flex items-center justify-center py-16">
                    <Loader2 className="w-8 h-8 text-primary-600 animate-spin" />
                </div>
            ) : graphs.length === 0 ? (
                <div className="text-center py-16 bg-surface rounded-xl border-2 border-dashed border-default">
                    <Clock className="w-10 h-10 text-muted mx-auto mb-3" />
                    <h3 className="text-base font-semibold text-default mb-1">
                        No delegation graphs in this window
                    </h3>
                    <p className="text-sm text-muted max-w-md mx-auto">
                        Graphs appear here when agents call each other through this gateway.
                        Widen the time window if you expected traffic.
                    </p>
                </div>
            ) : (
                <div className="bg-surface border border-default rounded-lg shadow-sm">
                    <ul className="divide-y divide-default">
                        {graphs.map(g => (
                            <GraphRow
                                key={g.root_task_id}
                                graph={g}
                                onOpen={setActiveRoot}
                                onOpenSystem={setActiveTab}
                            />
                        ))}
                    </ul>
                </div>
            )}

            {/* ============ PAGINATION ============ */}
            <div className="flex items-center justify-between text-sm">
                <div className="text-muted">
                    Showing {graphs.length === 0 ? 0 : offset + 1}–{offset + graphs.length} of{' '}
                    {total}
                </div>
                <div className="flex items-center space-x-2">
                    <button
                        onClick={() => setOffset(Math.max(0, offset - limit))}
                        disabled={offset === 0 || loading}
                        className="px-3 py-1.5 text-sm font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                        Prev
                    </button>
                    <button
                        onClick={() => setOffset(offset + limit)}
                        disabled={offset + limit >= total || loading}
                        className="px-3 py-1.5 text-sm font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                        Next
                    </button>
                    <select
                        value={limit}
                        onChange={e => setLimit(Number(e.target.value))}
                        className="appearance-none pl-3 pr-8 py-1.5 text-sm border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 bg-surface text-default"
                        aria-label="Page size"
                    >
                        <option value={25}>25 / page</option>
                        <option value={50}>50 / page</option>
                        <option value={100}>100 / page</option>
                    </select>
                </div>
            </div>
        </div>
    );
};

export default A2ADelegationGraphsTab;

// ────────────────────────────────────────────────────────────────────────────
// Sub-components
// ────────────────────────────────────────────────────────────────────────────

const StatsHeader: React.FC<{ stats: A2ADelegationStats | null; loading: boolean }> = ({
    stats,
    loading,
}) => {
    if (loading && !stats) {
        return (
            <div className="rounded-lg bg-surface border border-default p-4 text-secondary text-sm">
                <Loader2 className="w-4 h-4 animate-spin inline mr-2" />
                Loading stats…
            </div>
        );
    }
    if (!stats) return null;
    return (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            <KpiCard label="Graphs" value={formatInt(stats.total_graphs)} />
            <KpiCard label="Steps" value={formatInt(stats.total_steps)} />
            <KpiCard
                label="Success rate"
                value={formatPercent(stats.success_rate, 1)}
                icon={<Check className="w-3.5 h-3.5 text-green-600" />}
            />
            <KpiCard
                label="Failures"
                value={formatInt(stats.failure_steps)}
                icon={<XCircle className="w-3.5 h-3.5 text-red-600" />}
            />
            <KpiCard label="p50 duration" value={formatDurationMs(stats.p50_duration_ms)} />
            <KpiCard
                label="p95 duration"
                value={formatDurationMs(stats.p95_duration_ms)}
                icon={<TrendingUp className="w-3.5 h-3.5 text-primary-600" />}
            />
        </div>
    );
};

const KpiCard: React.FC<{ label: string; value: string; icon?: React.ReactNode }> = ({
    label,
    value,
    icon,
}) => (
    <div className="bg-surface border border-default rounded-lg p-3 shadow-sm">
        <div className="flex items-center space-x-1 text-xs uppercase tracking-wide text-muted">
            {icon}
            <span>{label}</span>
        </div>
        <div className="mt-1 text-lg font-semibold text-default">{value}</div>
    </div>
);

const FilterChip: React.FC<{ active: boolean; onClick: () => void; label: string }> = ({
    active,
    onClick,
    label,
}) => (
    <button
        type="button"
        onClick={onClick}
        className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors ${
            active
                ? 'bg-primary-500 text-white'
                : 'text-secondary border border-strong bg-surface hover:bg-surface-alt'
        }`}
    >
        {label}
    </button>
);

interface GraphRowProps {
    graph: A2ADelegationGraphSummary;
    onOpen: (rootTaskId: string) => void;
    onOpenSystem?: (tab: string) => void;
}

/**
 * The AI Systems a graph's run touched, root first, each a link to its
 * Composition tab. The click must not also open the graph detail.
 */
const GraphSystems: React.FC<{
    systems: NonNullable<A2ADelegationGraphSummary['systems']>;
    onOpenSystem?: (tab: string) => void;
}> = ({ systems, onOpenSystem }) => {
    const ordered = [...systems].sort((a, b) => (a.role === b.role ? 0 : a.role === 'root' ? -1 : 1));
    return (
        <div className="mt-1 flex flex-wrap items-center gap-1.5" data-testid="graph-systems">
            <span className="text-xs text-muted">AI Systems:</span>
            {ordered.map((s) => {
                const label = s.name ?? s.id;
                const title = s.role === 'root' ? 'Root system of this run' : 'Called as a shared service in this run';
                return onOpenSystem ? (
                    <button
                        key={s.id}
                        type="button"
                        title={title}
                        onClick={(e) => {
                            e.stopPropagation();
                            onOpenSystem(`ai-systems:${s.id}:composition`);
                        }}
                        className="rounded border border-default px-1.5 py-0.5 text-xs text-primary-700 hover:bg-primary-50"
                        data-testid={`graph-system-${s.id}`}
                    >
                        {label}
                        {s.role === 'root' && <span className="ml-1 text-muted">(root)</span>}
                    </button>
                ) : (
                    <span key={s.id} title={title} className="rounded border border-default px-1.5 py-0.5 text-xs text-secondary">
                        {label}
                    </span>
                );
            })}
        </div>
    );
};

const GraphRow: React.FC<GraphRowProps> = ({ graph, onOpen, onOpenSystem }) => (
    <li
        onClick={() => onOpen(graph.root_task_id)}
        className="cursor-pointer px-5 py-3 hover:bg-surface-alt transition-colors"
    >
        <div className="flex items-center justify-between gap-4">
            <div className="min-w-0 flex-1 min-h-0">
                <div className="flex items-center space-x-2">
                    <span
                        className={`w-2 h-2 rounded-full ${
                            graph.overall_success ? 'bg-green-500' : 'bg-red-500'
                        }`}
                        title={graph.overall_success ? 'Success' : 'Failure'}
                    />
                    <span className="flex items-center gap-1.5 text-sm font-medium text-default truncate">
                        <span className="truncate" title={graph.root_actor_agent_id ?? ''}>
                            {graph.root_actor_agent_name ?? 'caller'}
                        </span>
                        <span className="text-muted">{'→'}</span>
                        <span className="truncate text-primary-600" title={graph.root_card_id ?? ''}>
                            {graph.root_card_name ?? graph.root_card_id ?? 'callee'}
                        </span>
                    </span>
                    {graph.total_steps > 1 && (
                        <span className="px-1.5 py-0.5 text-xs rounded bg-primary-50 text-primary-700">
                            {graph.total_steps}-step chain
                        </span>
                    )}
                </div>
                <p className="mt-1 text-xs text-muted font-mono truncate" title={graph.root_task_id}>
                    {graph.root_task_id}
                </p>
                {graph.systems && graph.systems.length > 0 && (
                    <GraphSystems systems={graph.systems} onOpenSystem={onOpenSystem} />
                )}
            </div>
            <div className="flex items-start gap-5 text-xs text-muted whitespace-nowrap">
                <div className="text-right">
                    <div className="text-default font-medium">
                        {graph.total_steps} step{graph.total_steps === 1 ? '' : 's'}
                    </div>
                    <div>depth {graph.max_depth}</div>
                </div>
                <div className="text-right">
                    <div>{graph.inbound_steps} inbound</div>
                    <div>{graph.outbound_steps} outbound</div>
                </div>
                <div className="text-right">
                    <div>{formatDurationMs(graph.duration_ms)}</div>
                    <div title={formatDate(graph.started_at)}>
                        {formatRelative(graph.started_at)}
                    </div>
                </div>
            </div>
        </div>
    </li>
);

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useEffect, useState } from 'react';
import {
    Activity,
    CheckCircle2,
    XCircle,
    AlertTriangle,
    AlertCircle,
    Server,
    Database,
    Shield,
    Clock,
    ChevronDown,
    ChevronRight,
    Layers,
    Zap,
    BarChart3,
    PlayCircle,
    Share2,
} from 'lucide-react';
import type { RouterStatusResponse, RouterTestResponse } from '../../../lib/llmTypes';
import { useGateway } from '../../../hooks/useGateway';
import { ProviderLogo } from '@/components/llm/config/providerLogos';
import { TABLE_TH_TYPE } from '../../ui/tableStyles';
import { useReadFailures } from '../../../hooks/useRead';
import ReadFailureNotice from '../../common/ReadFailureNotice';

// ─── Strategy label map ──────────────────────────────────────────────────────

const STRATEGY_LABELS: Record<string, string> = {
    'simple-shuffle': 'Weighted Shuffle',
    'least-busy': 'Queue-Aware',
    'usage-based-routing': 'TPM/RPM Aware (v1)',
    'usage-based-routing-v2': 'Rate-Limit Optimized',
    'latency-based-routing': 'Fastest Response',
    'cost-based-routing': 'Cheapest Healthy',
    'complexity-based-routing': 'Prompt Complexity',
};

/**
 * Render an ISO timestamp as a short relative string ("3m ago",
 * "yesterday") for the recent-tests list. Falls through to the
 * raw ISO if the parse fails — better to show something
 * understandable than a confident-looking "Invalid Date".
 */
function formatTestTimestamp(iso: string): string {
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return iso;
    const dSec = Math.max(1, Math.round((Date.now() - t) / 1000));
    if (dSec < 60) return `${dSec}s ago`;
    if (dSec < 3600) return `${Math.round(dSec / 60)}m ago`;
    if (dSec < 86400) return `${Math.round(dSec / 3600)}h ago`;
    return new Date(t).toLocaleDateString();
}

// ─── Health helpers ──────────────────────────────────────────────────────────

const healthIcon = (status: string, enabled: boolean) => {
    if (!enabled) return { Icon: XCircle, color: 'text-muted', bg: 'bg-surface-raised', label: 'Disabled' };
    switch (status) {
        case 'healthy':  return { Icon: CheckCircle2, color: 'text-green-600', bg: 'bg-green-100', label: 'Healthy' };
        case 'unhealthy': return { Icon: XCircle, color: 'text-red-600', bg: 'bg-red-100', label: 'Unhealthy' };
        case 'degraded': return { Icon: AlertTriangle, color: 'text-yellow-600', bg: 'bg-yellow-100', label: 'Degraded' };
        default:         return { Icon: AlertCircle, color: 'text-muted', bg: 'bg-surface-raised', label: 'Unknown' };
    }
};

// ─── KPI card ────────────────────────────────────────────────────────────────

const KpiCard: React.FC<{
    label: string;
    value: string | number;
    sub?: string;
    icon: React.ElementType;
    color: string;
}> = ({ label, value, sub, icon: Icon, color }) => (
    <div className="bg-surface border border-default rounded-lg p-4 flex items-start space-x-3">
        <div className={`p-2 rounded-lg ${color}`}>
            <Icon className="w-5 h-5" />
        </div>
        <div>
            <p className="text-2xl font-bold text-default">{value}</p>
            <p className="text-xs text-muted">{label}</p>
            {sub && <p className="text-xs text-muted mt-0.5">{sub}</p>}
        </div>
    </div>
);

// ─── Progress bar ────────────────────────────────────────────────────────────

const ProgressBar: React.FC<{ value: number; max: number; color: string }> = ({ value, max, color }) => {
    const pct = max > 0 ? Math.min((value / max) * 100, 100) : 0;
    return (
        <div className="w-full h-2 bg-surface-inset rounded-full overflow-hidden">
            <div className={`h-full rounded-full transition-all ${color}`} style={{ width: `${pct}%` }} />
        </div>
    );
};

// ─── Props ───────────────────────────────────────────────────────────────────

interface RouterStatusSubTabProps {
    status: RouterStatusResponse | null;
}

// ─── Component ───────────────────────────────────────────────────────────────

const RouterStatusSubTab: React.FC<RouterStatusSubTabProps> = ({ status }) => {
    const { gatewayClient } = useGateway();
    const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
    // Per-group test history. Lazy-loaded the first time a
    // group is expanded — no point fetching N groups' worth of
    // history before the operator asks. Keyed by routing group
    // id; `null` = "fetched, none yet"; absent = "not fetched".
    const [testHistoryByGroup, setTestHistoryByGroup] = useState<
        Record<string, RouterTestResponse[] | null>
    >({});
    // The history of a group, by the id of the group.
    const [readFailure, reads] = useReadFailures();

    // Refetch whichever expanded groups exist when the parent
    // bumps `status` (i.e. on the Refresh button). Avoids the
    // case where the operator runs a test, hits Refresh, and
    // doesn't see the new entry because we cached the old list.
    useEffect(() => {
        if (!status) return;
        Object.keys(testHistoryByGroup).forEach((groupId) => {
            // Best-effort: any error just leaves the previous
            // list in place; the operator can re-expand or the
            // next Refresh retries.
            gatewayClient
                .getRouterTestHistory({ routing_group_id: groupId, limit: 10 })
                .then((resp) => {
                    setTestHistoryByGroup((prev) => ({
                        ...prev,
                        [groupId]: resp.items,
                    }));
                    reads.loaded(groupId);
                })
                .catch((err: unknown) => {
                    // The list shown stays, marked as stale.
                    reads.failed(groupId, 'Failed to load the test history', err);
                });
        });
        // The dep tracks status only; we deliberately don't
        // include the history map itself or the function would
        // refetch on every state update it triggers.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [status]);

    if (!status) {
        return (
            <div className="flex flex-col items-center justify-center py-20 text-muted">
                <Activity className="w-12 h-12 mb-3" />
                <p className="text-lg font-medium">No status data available</p>
                <p className="text-sm">Click Refresh to load router status</p>
            </div>
        );
    }

    const toggleGroup = (id: string) => {
        setExpandedGroups((prev) => {
            const next = new Set(prev);
            next.has(id) ? next.delete(id) : next.add(id);
            return next;
        });
        // Lazy-load history on first expand. We don't gate on
        // `expandedGroups.has(id)` because state updates above
        // are batched — read the current map directly.
        if (testHistoryByGroup[id] === undefined) loadTestHistory(id);
    };

    /** Ask for the test history of one group: on first expand, and again from the Retry of its notice. */
    const loadTestHistory = (id: string) => {
        gatewayClient
            .getRouterTestHistory({ routing_group_id: id, limit: 10 })
            .then((resp) => {
                setTestHistoryByGroup((p) => ({ ...p, [id]: resp.items }));
                reads.loaded(id);
            })
            .catch((err: unknown) => {
                // Held as an empty list so that collapsing and
                // expanding does not ask again; Retry and the next
                // refresh of the parent do. The row says it is not
                // "no tests".
                setTestHistoryByGroup((p) => ({ ...p, [id]: [] }));
                reads.lost(id, 'Failed to load the test history', err);
            });
    };

    const rc = status.router_cache;
    const groups = status.routing_groups || [];

    // ── Not enabled ──────────────────────────────────────────────────────
    if (!status.enabled) {
        return (
            <div className="flex flex-col items-center justify-center py-20">
                <div className="p-4 bg-yellow-100 rounded-full mb-4">
                    <AlertTriangle className="w-10 h-10 text-yellow-600" />
                </div>
                <h3 className="text-lg font-semibold text-default mb-1">Router Not Active</h3>
                <p className="text-sm text-secondary max-w-md text-center">
                    No routers have been built yet. Routers are created automatically when requests arrive.
                    Create a routing group, assign models, and send a request to activate.
                </p>
            </div>
        );
    }

    return (
        <div className="space-y-6 max-w-6xl">
            {/* ── KPI Row ─────────────────────────────────────────────────── */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                <KpiCard
                    label="Deployments"
                    value={status.total_deployments}
                    sub={`${status.healthy_deployments} healthy · ${status.unhealthy_deployments} unhealthy`}
                    icon={Server}
                    color="bg-blue-100 text-blue-600"
                />
                <KpiCard
                    label="Routing Groups"
                    value={status.total_routing_groups}
                    sub={`${status.routing_groups.length} active`}
                    icon={Share2}
                    color="bg-purple-100 text-purple-600"
                />
                <KpiCard
                    label="Total Requests"
                    value={status.total_requests.toLocaleString()}
                    sub={status.total_errors > 0 ? `${status.error_rate}% error rate` : 'No errors'}
                    icon={BarChart3}
                    color={status.error_rate > 5 ? 'bg-red-100 text-red-600' : 'bg-green-100 text-green-600'}
                />
                <KpiCard
                    label="Avg Latency"
                    value={status.avg_latency_ms > 0 ? `${Math.round(status.avg_latency_ms)}ms` : '—'}
                    sub="Weighted average across all models"
                    icon={Clock}
                    color="bg-yellow-100 text-yellow-600"
                />
            </div>

            {/* ── Routing Groups ───────────────────────────────────────────── */}
            {groups.length > 0 && (
                <section>
                    <h3 className="text-sm font-bold text-default uppercase tracking-wide mb-3">
                        Routing Groups
                    </h3>
                    <div className="space-y-3">
                        {groups.map(group => {
                            const expanded = expandedGroups.has(group.id);
                            const strategyLabel = STRATEGY_LABELS[group.routing_strategy] || group.routing_strategy;
                            const healthPct = group.total_models > 0
                                ? Math.round((group.healthy_models / group.total_models) * 100)
                                : 0;

                            return (
                                <div key={group.id} className="bg-surface border border-default rounded-lg overflow-hidden">
                                    {/* Header */}
                                    <button
                                        onClick={() => toggleGroup(group.id)}
                                        className="w-full flex items-center justify-between px-5 py-4 hover:bg-surface-alt transition-colors text-left"
                                    >
                                        <div className="flex items-center space-x-3 min-w-0">
                                            <div className="p-2 rounded-lg bg-primary-100">
                                                <Layers className="w-4 h-4 text-primary-600" />
                                            </div>
                                            <div className="min-w-0">
                                                <div className="flex items-center space-x-2">
                                                    <span className="font-semibold text-default text-sm truncate">{group.name}</span>
                                                </div>
                                                <div className="flex items-center space-x-3 text-xs text-muted mt-0.5">
                                                    <span>{strategyLabel}</span>
                                                    <span>·</span>
                                                    <span>{group.total_models} model{group.total_models !== 1 ? 's' : ''}</span>
                                                    {group.total_requests > 0 && (
                                                        <>
                                                            <span>·</span>
                                                            <span>{group.total_requests.toLocaleString()} req</span>
                                                        </>
                                                    )}
                                                </div>
                                            </div>
                                        </div>

                                        <div className="flex items-center space-x-4 flex-shrink-0">
                                            {/* Health bar mini */}
                                            <div className="hidden md:flex items-center space-x-2 w-32">
                                                <ProgressBar
                                                    value={group.healthy_models}
                                                    max={group.total_models}
                                                    color={healthPct === 100 ? 'bg-green-500' : healthPct >= 50 ? 'bg-yellow-500' : 'bg-red-500'}
                                                />
                                                <span className="text-xs text-muted whitespace-nowrap">{healthPct}%</span>
                                            </div>

                                            {/* Feature badges */}
                                            <div className="hidden md:flex items-center space-x-1">
                                                {group.has_fallbacks && (
                                                    <div className="p-1 bg-blue-50 rounded" title="Fallbacks configured">
                                                        <Shield className="w-3 h-3 text-blue-500" />
                                                    </div>
                                                )}
                                            </div>

                                            {/* Error rate */}
                                            {group.error_rate > 0 && (
                                                <span className={`text-xs font-medium px-2 py-0.5 rounded ${
                                                    group.error_rate > 10 ? 'bg-red-100 text-red-700' :
                                                        group.error_rate > 2 ? 'bg-yellow-100 text-yellow-700' :
                                                            'bg-surface-raised text-secondary'
                                                }`}>
                                                    {group.error_rate}% err
                                                </span>
                                            )}

                                            {expanded
                                                ? <ChevronDown className="w-4 h-4 text-muted" />
                                                : <ChevronRight className="w-4 h-4 text-muted" />
                                            }
                                        </div>
                                    </button>

                                    {/* Expanded: per-model detail */}
                                    {expanded && (
                                        <div className="border-t border-default bg-surface-alt">
                                            {/* Stats row — three columns, none of them
                                                cost. Spend lives in the resource-group
                                                Usage Limits surface where it applies
                                                uniformly to every request, including
                                                routing groups. */}
                                            <div className="grid grid-cols-3 gap-4 px-5 py-3 border-b border-default text-center">
                                                <div>
                                                    <p className="text-lg font-bold text-default">{group.healthy_models}/{group.total_models}</p>
                                                    <p className="text-[11px] text-muted">Healthy</p>
                                                </div>
                                                <div>
                                                    <p className="text-lg font-bold text-default">{group.total_requests.toLocaleString()}</p>
                                                    <p className="text-[11px] text-muted">Requests</p>
                                                </div>
                                                <div>
                                                    <p className="text-lg font-bold text-default">
                                                        {group.avg_latency_ms != null ? `${Math.round(group.avg_latency_ms)}ms` : '—'}
                                                    </p>
                                                    <p className="text-[11px] text-muted">Avg Latency</p>
                                                </div>
                                            </div>

                                            {/* Model table */}
                                            {group.models.length === 0 ? (
                                                <div className="px-5 py-6 text-center text-sm text-muted">
                                                    No models assigned to this routing group
                                                </div>
                                            ) : (
                                                <div className="divide-y divide-default">
                                                    {/* Table header */}
                                                    <div className={`grid grid-cols-12 gap-2 px-5 py-2 bg-surface-alt ${TABLE_TH_TYPE}`}>
                                                        <div className="col-span-1">Status</div>
                                                        <div className="col-span-4">Model</div>
                                                        <div className="col-span-1 text-right">Weight</div>
                                                        <div className="col-span-2 text-right">Requests</div>
                                                        <div className="col-span-2 text-right">Latency</div>
                                                        <div className="col-span-2 text-right">Error Rate</div>
                                                    </div>

                                                    {group.models.map(model => {
                                                        const h = healthIcon(model.health_status, true);
                                                        return (
                                                            <div key={model.model_id} className="grid grid-cols-12 gap-2 px-5 py-2.5 items-center hover:bg-surface-raised/50">
                                                                <div className="col-span-1">
                                                                    <div className={`inline-flex p-1 rounded ${h.bg}`}>
                                                                        <h.Icon className={`w-3.5 h-3.5 ${h.color}`} />
                                                                    </div>
                                                                </div>
                                                                <div className="col-span-4 flex items-center space-x-2 min-w-0">
                                                                    <ProviderLogo provider={model.provider} size="xs" />
                                                                    <div className="min-w-0">
                                                                        <p className="text-sm font-medium text-default truncate">{model.model_name}</p>
                                                                        <p className="text-[11px] text-muted">{model.provider}</p>
                                                                    </div>
                                                                </div>
                                                                <div className="col-span-1 text-right text-sm text-secondary">{model.routing_weight}</div>
                                                                <div className="col-span-2 text-right text-sm font-medium text-default">
                                                                    {model.total_requests.toLocaleString()}
                                                                </div>
                                                                <div className="col-span-2 text-right text-sm text-secondary">
                                                                    {model.avg_latency_ms != null ? `${Math.round(model.avg_latency_ms)}ms` : '—'}
                                                                </div>
                                                                <div className="col-span-2 text-right">
                                                                    <span className={`text-sm font-medium ${
                                                                        model.error_rate > 10 ? 'text-red-600' :
                                                                            model.error_rate > 2 ? 'text-yellow-600' :
                                                                                'text-secondary'
                                                                    }`}>
                                                                        {model.error_rate > 0 ? `${model.error_rate}%` : '0%'}
                                                                    </span>
                                                                </div>
                                                            </div>
                                                        );
                                                    })}
                                                </div>
                                            )}

                                            {/* Recent tests for this routing group.
                                                Populated by the Test Router tab —
                                                each Run Test entry lands here so
                                                operators can see "what did the
                                                router pick last time someone
                                                exercised this group". */}
                                            <div className="border-t border-default px-5 py-3">
                                                <div className="flex items-center space-x-2 mb-2">
                                                    <PlayCircle className="w-3.5 h-3.5 text-muted" />
                                                    <span className={TABLE_TH_TYPE}>
                                                        Recent tests
                                                    </span>
                                                </div>
                                                {testHistoryByGroup[group.id] === undefined ? (
                                                    <div className="text-xs text-muted">Loading…</div>
                                                ) : readFailure(group.id) && (testHistoryByGroup[group.id]?.length ?? 0) === 0 ? (
                                                    <ReadFailureNotice compact failure={readFailure(group.id)} onRetry={() => loadTestHistory(group.id)} />
                                                ) : (testHistoryByGroup[group.id]?.length ?? 0) === 0 ? (
                                                    <div className="text-xs text-muted">
                                                        No tests recorded yet. Run one from the{' '}
                                                        <em>Test Router</em> tab to see what the
                                                        router picks.
                                                    </div>
                                                ) : (
                                                    <div className="divide-y divide-default">
                                                        {testHistoryByGroup[group.id]!.map((entry, idx) => (
                                                            <div
                                                                key={`${entry.timestamp}-${idx}`}
                                                                className="grid grid-cols-12 gap-2 py-2 items-center text-xs"
                                                            >
                                                                <div className="col-span-3 text-muted">
                                                                    {formatTestTimestamp(entry.timestamp)}
                                                                </div>
                                                                <div className="col-span-3 truncate">
                                                                    {entry.error ? (
                                                                        <span className="text-red-700 inline-flex items-center space-x-1">
                                                                            <XCircle className="w-3 h-3" />
                                                                            <span>error</span>
                                                                        </span>
                                                                    ) : (
                                                                        <span className="text-default font-medium">
                                                                            {entry.selected_model || '<unknown>'}
                                                                        </span>
                                                                    )}
                                                                </div>
                                                                <div className="col-span-2 text-secondary">
                                                                    {entry.status_code ?? '—'}
                                                                </div>
                                                                <div className="col-span-2 text-secondary">
                                                                    {typeof entry.latency_ms === 'number'
                                                                        ? `${Math.round(entry.latency_ms)}ms`
                                                                        : '—'}
                                                                </div>
                                                                <div className="col-span-2 text-muted truncate" title={entry.message}>
                                                                    {entry.message}
                                                                </div>
                                                            </div>
                                                        ))}
                                                    </div>
                                                )}
                                            </div>
                                        </div>
                                    )}
                                </div>
                            );
                        })}
                    </div>
                </section>
            )}

            {/* ── Unassigned models callout ─────────────────────────────── */}
            {status.unassigned_deployments > 0 && (
                <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-4">
                    <div className="flex items-start space-x-3">
                        <AlertCircle className="w-5 h-5 text-yellow-600 mt-0.5 flex-shrink-0" />
                        <div>
                            <p className="text-sm font-semibold text-yellow-900">
                                {status.unassigned_deployments} model{status.unassigned_deployments !== 1 ? 's' : ''} not assigned to any routing group
                            </p>
                            <p className="text-xs text-yellow-700 mt-0.5">
                                These models can only be called directly and won't benefit from load balancing or failover.
                                Assign them to a routing group in the LLMs tab.
                            </p>
                        </div>
                    </div>
                </div>
            )}

            {/* ── Infrastructure row ───────────────────────────────────── */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {/* Router Cache */}
                <div className="bg-surface border border-default rounded-lg p-5">
                    <div className="flex items-center space-x-2 mb-4">
                        <Zap className="w-4 h-4 text-primary-600" />
                        <h4 className="text-sm font-bold text-default">Router Instance Cache</h4>
                    </div>
                    <div className="space-y-4">
                        {/* Utilization bar */}
                        <div>
                            <div className="flex items-center justify-between mb-1">
                                <span className="text-xs font-medium text-secondary">Cache Utilization</span>
                                <span className="text-xs font-semibold text-default">
                                    {rc.cached_routers} / {rc.max_cached_routers} slots
                                </span>
                            </div>
                            <ProgressBar
                                value={rc.cached_routers}
                                max={rc.max_cached_routers}
                                color={rc.cache_utilization > 0.8 ? 'bg-red-500' : rc.cache_utilization > 0.5 ? 'bg-yellow-500' : 'bg-primary-500'}
                            />
                            <p className="text-[11px] text-muted mt-1">
                                Each unique user/group model combination gets its own cached router instance. LRU eviction when full.
                            </p>
                        </div>

                        {/* Stats grid */}
                        <div className="space-y-2">
                            {[
                                {
                                    label: 'Hit Rate',
                                    value: `${(rc.hit_rate * 100).toFixed(1)}%`,
                                    description: 'Percentage of requests served by a cached router instead of building a new one.',
                                    good: rc.hit_rate >= 0.8,
                                    warn: rc.hit_rate >= 0.5 && rc.hit_rate < 0.8,
                                },
                                {
                                    label: 'Cache Hits',
                                    value: rc.cache_hits.toLocaleString(),
                                    description: 'Requests that reused an existing router — no rebuild overhead.',
                                    good: true,
                                    warn: false,
                                },
                                {
                                    label: 'Cache Misses',
                                    value: rc.cache_misses.toLocaleString(),
                                    description: 'Requests that required building a new router (first-time user/group combo or expired TTL).',
                                    good: false,
                                    warn: rc.cache_misses > 0,
                                },
                                {
                                    label: 'Total Rebuilds',
                                    value: rc.total_rebuilds.toLocaleString(),
                                    description: 'Total router construction events including misses, TTL expiry, and LRU evictions.',
                                    good: false,
                                    warn: rc.total_rebuilds > rc.cache_hits * 0.2,
                                },
                                {
                                    label: 'Router TTL',
                                    value: rc.router_ttl_seconds >= 3600
                                        ? `${(rc.router_ttl_seconds / 3600).toFixed(1)}h`
                                        : `${rc.router_ttl_seconds}s`,
                                    description: 'Time-to-live for each cached router. After expiry, the next request triggers a rebuild with fresh config.',
                                    good: true,
                                    warn: false,
                                },
                            ].map(item => (
                                <div key={item.label} className="flex items-start space-x-3 p-2.5 bg-surface-alt rounded-lg">
                                    <div className="flex-shrink-0 min-w-[72px] text-right">
                                        <span className={`text-sm font-bold ${
                                            item.warn ? 'text-yellow-600' : 'text-default'
                                        }`}>
                                            {item.value}
                                        </span>
                                    </div>
                                    <div className="min-w-0">
                                        <p className="text-xs font-medium text-secondary">{item.label}</p>
                                        <p className="text-[11px] text-muted leading-snug mt-0.5">{item.description}</p>
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>
                </div>

                {/* Infrastructure Status */}
                <div className="bg-surface border border-default rounded-lg p-5">
                    <div className="flex items-center space-x-2 mb-4">
                        <Activity className="w-4 h-4 text-primary-600" />
                        <h4 className="text-sm font-bold text-default">Infrastructure</h4>
                    </div>
                    <div className="space-y-2.5">
                        {[
                            {
                                label: 'Response Cache (Redis)',
                                active: status.cache_enabled,
                                detail: status.cache_enabled
                                    ? `Connected${status.redis_url ? ' · ' + status.redis_url.replace(/\/\/.*:.*@/, '//***@') : ''}`
                                    : 'Not configured',
                                icon: Database,
                            },
                            {
                                label: 'Automatic Fallback',
                                active: status.fallback_enabled,
                                detail: status.fallback_enabled ? 'Enabled' : 'Disabled',
                                icon: Shield,
                            },
                            {
                                label: 'Health Checks',
                                active: status.health_check_enabled,
                                detail: status.health_check_enabled ? 'Active' : 'Inactive',
                                icon: Activity,
                            },
                            {
                                label: 'Default Strategy',
                                active: true,
                                detail: STRATEGY_LABELS[status.routing_strategy] || status.routing_strategy,
                                icon: Share2,
                            },
                        ].map(item => (
                            <div key={item.label} className="flex items-center space-x-3 py-1.5">
                                <div className={`p-1.5 rounded ${item.active ? 'bg-green-100' : 'bg-surface-raised'}`}>
                                    <item.icon className={`w-3.5 h-3.5 ${item.active ? 'text-green-600' : 'text-muted'}`} />
                                </div>
                                <div className="flex-1 min-w-0 min-h-0">
                                    <p className="text-xs font-medium text-default">{item.label}</p>
                                    <p className="text-[11px] text-muted truncate">{item.detail}</p>
                                </div>
                                <div className={`w-2 h-2 rounded-full flex-shrink-0 ${item.active ? 'bg-green-500' : 'bg-surface-inset'}`} />
                            </div>
                        ))}
                    </div>
                </div>
            </div>
        </div>
    );
};

export default RouterStatusSubTab;
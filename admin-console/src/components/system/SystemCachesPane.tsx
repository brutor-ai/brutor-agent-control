/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
    DatabaseZap,
    Brain,
    Shield,
    Users as UsersIcon,
    Wrench,
    Plug,
    RefreshCw,
    Trash2,
    Lock,
    X,
    AlertTriangle,
} from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { useAuth } from '../../hooks/useAuth';
import {
    CacheFlushRequest,
    CacheLayer,
    CacheLayerStats,
    CacheScope,
    CacheStatsResponse,
} from '../../lib/systemOpsTypes';
import { errorMessage } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';

/**
 * SystemCachesPane.
 *
 * One card per cache layer (5 today). Each card shows a hit/miss
 * gauge, the approximate item count when available, the timestamp of
 * the last flush from the audit log, and a Flush button that opens a
 * scope-picker modal. The modal restricts the operator to the
 * scope kinds the backend declared `scopes_available` for the layer
 * — this is the only client-side validation against an invalid
 * (layer, scope) pair; the backend's CacheFlushRequest model is the
 * server-side gate.
 *
 * Polling: the page fetches on mount and offers a manual Refresh
 * button, but does NOT auto-poll. Cache stats change far less
 * dramatically than Overview probes; an operator who came here to
 * decide whether to flush isn't watching the numbers wiggle. Removing
 * the timer keeps the page cheap and avoids the "did the number just
 * change because of my flush, or because of traffic?" cognitive load.
 *
 * Permission gating: SYSTEM_OPERATE controls whether the Flush
 * button is enabled. Without it the page is read-only and the button
 * renders with a lock icon + tooltip pointing at the missing perm.
 * SYSTEM_READ is checked one level up by SystemConsoleTab.
 */

const PERM_OPERATE = 'system:operate';

interface Props {
    /**
     * Receive deep-link clicks from this pane back into the System
     * Console tab dispatch — kept as a prop for parity with the
     * other System sub-section components even though Caches doesn't
     * currently surface any deep-links.
     */
    onTabChange?: (tab: string) => void;
}

const SystemCachesPane: React.FC<Props> = () => {
    const { gatewayClient } = useGateway();
    const { hasPermission } = useAuth();
    const canOperate = hasPermission(PERM_OPERATE);

    const [data, setData] = useState<CacheStatsResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [flushTarget, setFlushTarget] = useState<CacheLayerStats | null>(null);
    const [flushError, setFlushError] = useState<string | null>(null);

    const fetchStats = useCallback(async () => {
        try {
            const resp = await gatewayClient.listSystemCaches();
            setData(resp);
            setError(null);
        } catch (e) {
            setError(errorMessage(e) || String(e));
        } finally {
            setLoading(false);
        }
    }, [gatewayClient]);

    // Load when the pane mounts, and again when the client is another one.
    // The client is one object for the life of a session, so this runs once.
    useEffect(() => {
        runBackground(fetchStats(), 'stats');
    }, [fetchStats]);

    const onFlush = useCallback(
        async (req: CacheFlushRequest) => {
            // Bubble the publish-time errors into the modal so a
            // misbuilt payload doesn't silently no-op (which is
            // exactly the failure mode the channel registry on the
            // server side is designed to surface).
            try {
                await gatewayClient.flushSystemCache(req);
                setFlushTarget(null);
                setFlushError(null);
                // Re-fetch so the "Last flushed" timestamp updates
                // immediately rather than waiting for the next
                // manual refresh.
                runBackground(fetchStats(), 'stats');
            } catch (e) {
                setFlushError(errorMessage(e) || String(e));
            }
        },
        [gatewayClient, fetchStats]
    );

    if (loading && !data) {
        return <div className="text-secondary text-sm">Loading cache stats…</div>;
    }

    if (error && !data) {
        return (
            <div
                data-testid="system-caches-error"
                className="bg-red-50 border border-red-200 text-red-800 p-4 rounded-md max-w-2xl"
            >
                <div className="font-semibold">Couldn't load cache stats</div>
                <div className="text-sm mt-1">{error}</div>
                <button
                    type="button"
                    className="mt-3 px-3 py-1.5 text-sm rounded border border-red-300 hover:bg-red-100"
                    onClick={asyncHandler(fetchStats, 'click')}
                >
                    Retry
                </button>
            </div>
        );
    }

    if (!data) return null;

    return (
        <div className="space-y-6 max-w-6xl">
            <header className="flex flex-wrap items-start gap-4 justify-between">
                <div>
                    <h2 className="text-base font-semibold text-default flex items-center gap-2">
                        <DatabaseZap className="w-4 h-4 text-primary" />
                        Cache Layers
                    </h2>
                    <p className="text-xs text-secondary mt-1 max-w-2xl">
                        Stats and flush controls for the 5 cache layers the proxy maintains. Hit-rate
                        numbers reflect the last 60 minutes of proxy traffic where available; layers held in each gateway replica's memory show a note instead of a hit rate, because no combined figure exists for them.
                    </p>
                </div>
                <button
                    type="button"
                    onClick={asyncHandler(fetchStats, 'click')}
                    data-testid="system-caches-refresh"
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-default hover:bg-surface-muted"
                >
                    <RefreshCw className="w-4 h-4" />
                    Refresh
                </button>
            </header>

            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {data.layers.map(layer => (
                    <CacheLayerCard
                        key={layer.layer}
                        layer={layer}
                        canOperate={canOperate}
                        onFlush={() => setFlushTarget(layer)}
                    />
                ))}
            </div>

            <footer className="text-xs text-tertiary pt-2 border-t border-default">
                Generated {formatTimestamp(data.generated_at)}
            </footer>

            {flushTarget && (
                <FlushModal
                    layer={flushTarget}
                    error={flushError}
                    onCancel={() => {
                        setFlushTarget(null);
                        setFlushError(null);
                    }}
                    onConfirm={asyncHandler(onFlush, 'confirm')}
                />
            )}
        </div>
    );
};

// ─── Per-layer card ──────────────────────────────────────────────

const CacheLayerCard: React.FC<{
    layer: CacheLayerStats;
    canOperate: boolean;
    onFlush: () => void;
}> = ({ layer, canOperate, onFlush }) => {
    const ratePct = Math.round(layer.hit_rate * 1000) / 10; // 1 decimal
    const total = layer.hit_count + layer.miss_count;
    return (
        <div
            data-testid={`system-caches-card-${layer.layer}`}
            className="p-3 rounded-md border border-default bg-surface"
        >
            <div className="flex items-start gap-2">
                <div className="flex-shrink-0 mt-0.5">
                    {layerIcon(layer.layer)}
                </div>
                <div className="min-w-0 flex-1 min-h-0">
                    <div className="flex items-center justify-between gap-2">
                        <span className="text-sm font-medium text-default capitalize">
                            {layer.layer}
                        </span>
                        <span className="text-[11px] text-tertiary uppercase tracking-wide">
                            {total > 0 ? `${ratePct}% hit` : 'idle'}
                        </span>
                    </div>

                    {/* Hit-rate gauge. Empty rail when total=0 so a
                        long-idle layer doesn't look like 0% hit-rate
                        (it's "no traffic", not "everything's a miss"). */}
                    <div className="mt-2 h-1.5 rounded bg-surface-muted overflow-hidden">
                        {total > 0 ? (
                            <div
                                data-testid={`system-caches-gauge-${layer.layer}`}
                                className="h-full bg-emerald-500"
                                style={{ width: `${ratePct}%` }}
                            />
                        ) : null}
                    </div>

                    <dl className="mt-3 text-xs space-y-1">
                        <div className="flex justify-between">
                            <dt className="text-secondary">Hits</dt>
                            <dd className="font-mono">{layer.hit_count.toLocaleString()}</dd>
                        </div>
                        <div className="flex justify-between">
                            <dt className="text-secondary">Misses</dt>
                            <dd className="font-mono">{layer.miss_count.toLocaleString()}</dd>
                        </div>
                        {layer.item_count_approx !== null && (
                            <div className="flex justify-between">
                                <dt className="text-secondary">Items (approx.)</dt>
                                <dd className="font-mono">
                                    {layer.item_count_approx.toLocaleString()}
                                </dd>
                            </div>
                        )}
                        <div className="flex justify-between">
                            <dt className="text-secondary">Last flushed</dt>
                            <dd className="font-mono">
                                {layer.last_flushed_at
                                    ? formatTimestamp(layer.last_flushed_at)
                                    : 'never'}
                            </dd>
                        </div>
                    </dl>

                    {layer.detail && (
                        <div
                            data-testid={`system-caches-detail-${layer.layer}`}
                            className="mt-2 text-[11px] text-tertiary italic"
                        >
                            {layer.detail}
                        </div>
                    )}

                    <div className="mt-3 flex items-center gap-2">
                        <button
                            type="button"
                            disabled={!canOperate}
                            onClick={onFlush}
                            data-testid={`system-caches-flush-${layer.layer}`}
                            title={
                                canOperate
                                    ? `Flush the ${layer.layer} cache`
                                    : `Requires the ${PERM_OPERATE} permission`
                            }
                            className={`inline-flex items-center gap-1.5 px-2.5 py-1 text-xs rounded border ${
                                canOperate
                                    ? 'border-red-300 text-red-700 hover:bg-red-50 dark:border-red-700 dark:text-red-300 dark:hover:bg-red-900/20'
                                    : 'border-default text-tertiary cursor-not-allowed opacity-60'
                            }`}
                        >
                            {!canOperate ? (
                                <Lock className="w-3 h-3" />
                            ) : (
                                <Trash2 className="w-3 h-3" />
                            )}
                            Flush
                        </button>
                    </div>
                </div>
            </div>
        </div>
    );
};

// ─── Scope-picker modal ──────────────────────────────────────────

interface FlushModalProps {
    layer: CacheLayerStats;
    error: string | null;
    onCancel: () => void;
    onConfirm: (req: CacheFlushRequest) => void;
}

const FlushModal: React.FC<FlushModalProps> = ({
    layer,
    error,
    onCancel,
    onConfirm,
}) => {
    // Default to the first scope the backend declared available so
    // the operator's first selection is always valid. Falling back
    // to 'tenant' on an empty list keeps the form usable even if the
    // backend somehow returned a stats row with no scopes (shouldn't
    // happen, but defensive against a future schema regression).
    const initialScope: CacheScope = useMemo(
        () => (layer.scopes_available[0] ?? 'tenant'),
        [layer]
    );
    const [scope, setScope] = useState<CacheScope>(initialScope);
    const [id, setId] = useState('');
    const [reason, setReason] = useState('');

    const trimmedId = id.trim();
    const canConfirm = trimmedId.length > 0;

    return (
        <div
            role="dialog"
            aria-modal="true"
            data-testid="system-caches-flush-modal"
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
            onClick={onCancel}
        >
            <div
                className="bg-surface rounded-lg shadow-xl border border-default w-full max-w-lg"
                onClick={e => e.stopPropagation()}
            >
                <div className="flex items-center justify-between px-5 py-3 border-b border-default">
                    <h3 className="text-base font-semibold text-default flex items-center gap-2">
                        <AlertTriangle className="w-4 h-4 text-amber-600" />
                        Flush <span className="capitalize">{layer.layer}</span> cache
                    </h3>
                    <button
                        type="button"
                        onClick={onCancel}
                        data-testid="system-caches-flush-cancel"
                        className="text-secondary hover:text-default"
                    >
                        <X className="w-4 h-4" />
                    </button>
                </div>

                <div className="px-5 py-4 space-y-4">
                    <p className="text-sm text-secondary">
                        Flushing tells every gateway replica to drop the selected entries. The action is audited before it is sent, so it is on record even if a replica is briefly offline.
                    </p>

                    <div>
                        <label className="block text-xs font-medium text-secondary mb-1">
                            Scope
                        </label>
                        <select
                            data-testid="system-caches-flush-scope"
                            className="w-full border border-default rounded px-2 py-1.5 text-sm bg-surface"
                            value={scope}
                            onChange={e => setScope(e.target.value as CacheScope)}
                        >
                            {layer.scopes_available.map(s => (
                                <option key={s} value={s}>
                                    {s}
                                </option>
                            ))}
                        </select>
                        <p className="text-[11px] text-tertiary mt-1">
                            {scopeHint(scope)}
                        </p>
                    </div>

                    <div>
                        <label className="block text-xs font-medium text-secondary mb-1">
                            {scopeIdLabel(scope)}
                        </label>
                        <input
                            type="text"
                            value={id}
                            onChange={e => setId(e.target.value)}
                            data-testid="system-caches-flush-id"
                            placeholder={scopeIdPlaceholder(scope)}
                            className="w-full border border-default rounded px-2 py-1.5 text-sm bg-surface font-mono"
                        />
                    </div>

                    <div>
                        <label className="block text-xs font-medium text-secondary mb-1">
                            Reason (optional, audited)
                        </label>
                        <textarea
                            value={reason}
                            onChange={e => setReason(e.target.value)}
                            data-testid="system-caches-flush-reason"
                            rows={2}
                            className="w-full border border-default rounded px-2 py-1.5 text-sm bg-surface"
                            placeholder="e.g. RAG content rebuild — drop stale answers"
                        />
                    </div>

                    {error && (
                        <div
                            data-testid="system-caches-flush-error"
                            className="text-sm bg-red-50 border border-red-200 text-red-800 rounded p-2"
                        >
                            {error}
                        </div>
                    )}
                </div>

                <div className="px-5 py-3 border-t border-default flex items-center justify-end gap-2 bg-surface-muted/30">
                    <button
                        type="button"
                        onClick={onCancel}
                        className="px-3 py-1.5 text-sm rounded border border-default hover:bg-surface-muted"
                    >
                        Cancel
                    </button>
                    <button
                        type="button"
                        disabled={!canConfirm}
                        data-testid="system-caches-flush-confirm"
                        onClick={() =>
                            onConfirm({
                                layer: layer.layer,
                                scope,
                                id: trimmedId,
                                reason: reason.trim() || null,
                            })
                        }
                        className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded ${
                            canConfirm
                                ? 'bg-red-600 text-white hover:bg-red-700'
                                : 'bg-red-300 text-white cursor-not-allowed'
                        }`}
                    >
                        <Trash2 className="w-3.5 h-3.5" />
                        Flush
                    </button>
                </div>
            </div>
        </div>
    );
};

// ─── Visual helpers ──────────────────────────────────────────────

function layerIcon(layer: CacheLayer): React.ReactNode {
    switch (layer) {
        case 'semantic':
            return <Brain className="w-4 h-4 text-secondary" />;
        case 'policy':
            return <Shield className="w-4 h-4 text-secondary" />;
        case 'group':
            return <UsersIcon className="w-4 h-4 text-secondary" />;
        case 'tool':
            return <Plug className="w-4 h-4 text-secondary" />;
        case 'response':
            return <Wrench className="w-4 h-4 text-secondary" />;
        default:
            return <DatabaseZap className="w-4 h-4 text-secondary" />;
    }
}

function scopeHint(scope: CacheScope): string {
    switch (scope) {
        case 'tenant':
            return 'Drops every entry for this tenant. Use for incident recovery.';
        case 'group':
            return 'Drops entries belonging to one resource group.';
        case 'model':
            return 'Drops entries that targeted a specific model id (e.g. after retirement).';
        case 'mcp_server':
            return 'Drops the cached tool list for one MCP server.';
        case 'config':
            return 'Drops the routing-group response cache config — forces re-read.';
        default:
            return '';
    }
}

function scopeIdLabel(scope: CacheScope): string {
    switch (scope) {
        case 'tenant':
            return 'Tenant id';
        case 'group':
            return 'Resource group id';
        case 'model':
            return 'Model id';
        case 'mcp_server':
            return 'MCP server id';
        case 'config':
            return 'Config id (routing group id)';
    }
}

function scopeIdPlaceholder(scope: CacheScope): string {
    switch (scope) {
        case 'tenant':
            return 'tenant-acme';
        case 'group':
            return 'grp-default';
        case 'model':
            return 'llm-gpt-5';
        case 'mcp_server':
            return 'mcp-jira';
        case 'config':
            return 'rg-routing-prod';
    }
}

function formatTimestamp(iso: string | null): string {
    if (!iso) return 'never';
    try {
        return new Date(iso).toLocaleString();
    } catch {
        return iso;
    }
}

export default SystemCachesPane;

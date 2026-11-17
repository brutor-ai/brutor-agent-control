/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
    Database,
    Server,
    Brain,
    RefreshCw,
    AlertCircle,
    RotateCw,
    Lock,
} from 'lucide-react';

import { useAuth } from '../../hooks/useAuth';
import { useGateway } from '../../hooks/useGateway';
import {
    ConnectionListResponse,
    ConnectionPool,
} from '../../lib/systemOpsTypes';
import { PERM_OPERATE } from './systemPermissions';
import { errorMessage } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';

/**
 * SystemConnectionsPane.
 *
 * Renders one card per outbound pool the control plane knows about:
 * Postgres (real pool stats), Redis (intentionally 'unknown'),
 * Qdrant (per-request client + recent alert), and one entry per
 * configured LLM provider (pointing the operator at the Rust proxy's
 * metrics endpoint for actual pool numbers).
 *
 * Saturation bar: each card shows a coloured bar = checked_out /
 * size. The colour scales: under 0.6 → emerald, 0.6–0.85 → amber,
 * above 0.85 → red. Indeterminate (size=null) renders a slate stripe
 * so the bar slot stays visually consistent across card types.
 *
 * Recycle button: gated on `system:operate` AND `pool.recyclable`
 * AND `kind === 'postgres'` (defence-in-depth — Redis recycle is a
 * noop today; we hide rather than render a confusing button). The
 * tooltip explains why on every disabled path so the operator never
 * sees a silently-greyed control.
 *
 * Auto-refresh: same 15s + Page Visibility pause pattern the
 * Overview pane uses, so an operator flipping between System tabs
 * doesn't blow extra API calls.
 */

const POLL_INTERVAL_MS = 15_000;

interface Props {
    /** Optional callback the parent uses to refresh sub-tab state. */
    onTabChange?: (tab: string) => void;
}

const SystemConnectionsPane: React.FC<Props> = () => {
    const { gatewayClient } = useGateway();
    const { hasPermission } = useAuth();

    const [data, setData] = useState<ConnectionListResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [lastFetchedAt, setLastFetchedAt] = useState<Date | null>(null);

    // Per-pool recycle state. Keyed by `kind` so two simultaneous
    // recycles (Postgres + Redis) don't share a spinner.
    const [recycling, setRecycling] = useState<Record<string, boolean>>({});
    const [recycleResult, setRecycleResult] = useState<
        Record<string, { status: string; detail: string }>
    >({});

    const canOperate = hasPermission(PERM_OPERATE);

    const fetchPools = useCallback(async () => {
        try {
            const resp = await gatewayClient.listSystemConnections();
            setData(resp);
            setError(null);
            setLastFetchedAt(new Date());
        } catch (e) {
            setError(errorMessage(e) || String(e));
        } finally {
            setLoading(false);
        }
    }, [gatewayClient]);

    // Initial fetch + polling. `fetchPools` is intentionally omitted
    // from the dep array — gatewayClient identity flips each render,
    // which would loop the effect.
     
    useEffect(() => {
        runBackground(fetchPools(), 'pools');
        const id = setInterval(() => {
            if (document.visibilityState === 'visible') {
                runBackground(fetchPools(), 'pools');
            }
        }, POLL_INTERVAL_MS);
        return () => clearInterval(id);
    }, []);

    const recyclePool = useCallback(
        async (kind: 'postgres' | 'redis') => {
            setRecycling(prev => ({ ...prev, [kind]: true }));
            try {
                const resp = await gatewayClient.recycleSystemPool({ kind });
                setRecycleResult(prev => ({
                    ...prev,
                    [kind]: {
                        status: resp.status ?? 'ok',
                        detail: resp.detail ?? '',
                    },
                }));
                // Reload pool stats so saturation reflects the new state.
                await fetchPools();
            } catch (e) {
                setRecycleResult(prev => ({
                    ...prev,
                    [kind]: {
                        status: 'error',
                        detail: errorMessage(e) || String(e),
                    },
                }));
            } finally {
                setRecycling(prev => ({ ...prev, [kind]: false }));
            }
        },
        [gatewayClient, fetchPools],
    );

    const grouped = useMemo(() => groupPools(data?.pools ?? []), [data]);

    if (loading && !data) {
        return (
            <div className="text-secondary text-sm">Loading pools…</div>
        );
    }

    if (error && !data) {
        return (
            <div
                data-testid="system-connections-error"
                className="bg-red-50 border border-red-200 text-red-800 p-4 rounded-md max-w-2xl"
            >
                <div className="font-semibold">Couldn't load pools</div>
                <div className="text-sm mt-1">{error}</div>
                <button
                    type="button"
                    className="mt-3 px-3 py-1.5 text-sm rounded border border-red-300 hover:bg-red-100"
                    onClick={asyncHandler(fetchPools, 'click')}
                >
                    Retry
                </button>
            </div>
        );
    }

    if (!data) return null;

    return (
        <div className="space-y-6 max-w-6xl">
            {/* Header strip */}
            <header className="flex items-start gap-4 justify-between">
                <div>
                    <h2 className="text-base font-semibold text-default">
                        Outbound pools
                    </h2>
                    <p className="text-xs text-secondary">
                        One card per outbound dependency. Saturation =
                        checked-out / size. Recycle disposes the pool;
                        next checkout opens a fresh connection.
                    </p>
                </div>
                <button
                    type="button"
                    onClick={asyncHandler(fetchPools, 'click')}
                    data-testid="system-connections-refresh"
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-default hover:bg-surface-muted"
                    title={
                        lastFetchedAt
                            ? `Last refreshed ${lastFetchedAt.toLocaleTimeString()}`
                            : 'Refresh'
                    }
                >
                    <RefreshCw className="w-4 h-4" />
                    Refresh
                </button>
            </header>

            <PoolGroup
                title="Infrastructure"
                description="Pools the control plane talks to on every request."
                pools={grouped.infra}
                canOperate={canOperate}
                recycling={recycling}
                onRecycle={asyncHandler(recyclePool, 'recycle')}
                recycleResult={recycleResult}
            />
            <PoolGroup
                title="LLM Providers"
                description="One entry per configured provider. Live connection-pool statistics are not shown here — this list shows which providers exist."
                pools={grouped.llm}
                canOperate={canOperate}
                recycling={recycling}
                onRecycle={asyncHandler(recyclePool, 'recycle')}
                recycleResult={recycleResult}
                emptyHint="No LLM providers configured."
            />

            <footer className="text-xs text-tertiary pt-2 border-t border-default">
                Generated {formatTimestamp(data.generated_at)}
                {lastFetchedAt && (
                    <> · client refreshed {lastFetchedAt.toLocaleTimeString()}</>
                )}
            </footer>
        </div>
    );
};

// ─── Pool group ──────────────────────────────────────────────────

interface PoolGroupProps {
    title: string;
    description: string;
    pools: ConnectionPool[];
    canOperate: boolean;
    recycling: Record<string, boolean>;
    onRecycle: (kind: 'postgres' | 'redis') => void;
    recycleResult: Record<string, { status: string; detail: string }>;
    emptyHint?: string;
}

const PoolGroup: React.FC<PoolGroupProps> = ({
    title,
    description,
    pools,
    canOperate,
    recycling,
    onRecycle,
    recycleResult,
    emptyHint,
}) => (
    <section>
        <div className="mb-3">
            <h2 className="text-base font-semibold text-default">{title}</h2>
            <p className="text-xs text-secondary">{description}</p>
        </div>
        {pools.length === 0 ? (
            <div
                data-testid={`system-connections-group-empty-${title.toLowerCase().replace(/\s+/g, '-')}`}
                className="text-sm text-tertiary italic"
            >
                {emptyHint ?? 'No pools.'}
            </div>
        ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {pools.map(p => (
                    <PoolCard
                        key={p.kind}
                        pool={p}
                        canOperate={canOperate}
                        busy={!!recycling[p.kind]}
                        onRecycle={
                            p.kind === 'postgres' || p.kind === 'redis'
                                ? () => onRecycle(p.kind as 'postgres' | 'redis')
                                : undefined
                        }
                        result={recycleResult[p.kind]}
                    />
                ))}
            </div>
        )}
    </section>
);

// ─── Pool card ───────────────────────────────────────────────────

interface PoolCardProps {
    pool: ConnectionPool;
    canOperate: boolean;
    busy: boolean;
    onRecycle?: () => void;
    result?: { status: string; detail: string };
}

const PoolCard: React.FC<PoolCardProps> = ({
    pool,
    canOperate,
    busy,
    onRecycle,
    result,
}) => {
    // Recycle button is only meaningful for postgres today — redis is
    // a noop. We surface a disabled+labelled button for redis so the
    // operator can see the control exists but isn't wired, and a
    // disabled+permission-tooltip for the operator-permission case.
    const isRecyclable = pool.recyclable && pool.kind === 'postgres';
    const showRecycle = pool.kind === 'postgres' || pool.kind === 'redis';
    const recycleEnabled = isRecyclable && canOperate && !busy;
    const recycleTooltip = !canOperate
        ? `Requires the ${PERM_OPERATE} permission`
        : !pool.recyclable
        ? 'Recycle is a noop for this pool kind today'
        : busy
        ? 'Recycle in progress…'
        : 'Dispose the pool — next checkout opens a fresh connection.';

    return (
        <div
            data-testid={`system-connections-card-${pool.kind}`}
            className="p-4 rounded-md border border-default bg-surface space-y-3"
        >
            <div className="flex items-start gap-2">
                <div className="flex-shrink-0 mt-0.5">{kindIcon(pool.kind)}</div>
                <div className="min-w-0 flex-1 min-h-0">
                    <div className="text-sm font-medium text-default truncate">
                        {pool.label}
                    </div>
                    <div className="text-xs text-secondary mt-0.5 break-words">
                        {pool.detail}
                    </div>
                </div>
            </div>

            {/* Saturation bar — single source of UI truth for pressure. */}
            <SaturationBar pool={pool} />

            {/* Pool stats row — only meaningful when populated. */}
            <PoolStats pool={pool} />

            {/* Recycle action + result. */}
            {showRecycle && (
                <div className="flex items-center justify-between border-t border-default pt-3">
                    <button
                        type="button"
                        onClick={recycleEnabled ? onRecycle : undefined}
                        disabled={!recycleEnabled}
                        data-testid={`system-connections-recycle-${pool.kind}`}
                        title={recycleTooltip}
                        className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border ${
                            recycleEnabled
                                ? 'border-default hover:bg-surface-muted'
                                : 'border-default opacity-50 cursor-not-allowed'
                        }`}
                    >
                        {!canOperate && <Lock className="w-3.5 h-3.5" />}
                        <RotateCw
                            className={`w-3.5 h-3.5 ${busy ? 'animate-spin' : ''}`}
                        />
                        Recycle
                    </button>
                    {result && (
                        <div
                            data-testid={`system-connections-recycle-result-${pool.kind}`}
                            className={`text-xs ${
                                result.status === 'error'
                                    ? 'text-red-700'
                                    : 'text-secondary'
                            }`}
                        >
                            <span className="font-medium">{result.status}</span>
                            {result.detail && (
                                <span className="ml-1">{result.detail}</span>
                            )}
                        </div>
                    )}
                </div>
            )}
        </div>
    );
};

// ─── Saturation bar ──────────────────────────────────────────────

const SaturationBar: React.FC<{ pool: ConnectionPool }> = ({ pool }) => {
    const saturation = pool.saturation;
    // Indeterminate state: render a faded stripe so the bar slot
    // doesn't collapse and the card heights stay uniform.
    if (saturation === null || saturation === undefined) {
        return (
            <div
                data-testid={`system-connections-saturation-${pool.kind}`}
                className="h-2 rounded bg-slate-100 dark:bg-slate-800 relative overflow-hidden"
            >
                <div className="absolute inset-0 opacity-40 bg-gradient-to-r from-slate-300 via-transparent to-slate-300" />
                <span className="sr-only">Saturation unknown</span>
            </div>
        );
    }

    const pct = Math.min(saturation * 100, 100);
    const overflowing = saturation > 1;
    const colour = overflowing
        ? 'bg-red-500'
        : saturation > 0.85
        ? 'bg-red-500'
        : saturation > 0.6
        ? 'bg-amber-500'
        : 'bg-emerald-500';

    return (
        <div className="space-y-1">
            <div
                data-testid={`system-connections-saturation-${pool.kind}`}
                className="h-2 rounded bg-slate-100 dark:bg-slate-800 overflow-hidden"
            >
                <div
                    className={`h-full ${colour} transition-all`}
                    style={{ width: `${pct}%` }}
                />
            </div>
            <div className="flex items-center justify-between text-[11px] text-tertiary">
                <span>{(saturation * 100).toFixed(0)}% in use</span>
                {overflowing && (
                    <span className="inline-flex items-center gap-1 text-red-700 font-medium">
                        <AlertCircle className="w-3 h-3" /> overflowed
                    </span>
                )}
            </div>
        </div>
    );
};

// ─── Pool stats row ──────────────────────────────────────────────

const PoolStats: React.FC<{ pool: ConnectionPool }> = ({ pool }) => {
    // Build the visible stat list so empty (all-null) cards just
    // skip this row entirely.
    const items: { label: string; value: string | number }[] = [];
    if (pool.size !== null && pool.size !== undefined)
        items.push({ label: 'size', value: pool.size });
    if (pool.checked_in !== null && pool.checked_in !== undefined)
        items.push({ label: 'in', value: pool.checked_in });
    if (pool.checked_out !== null && pool.checked_out !== undefined)
        items.push({ label: 'out', value: pool.checked_out });
    if (pool.overflow !== null && pool.overflow !== undefined)
        items.push({ label: 'overflow', value: pool.overflow });
    if (pool.client_count !== null && pool.client_count !== undefined)
        items.push({ label: 'clients', value: pool.client_count });
    if (
        pool.most_recent_latency_ms !== null &&
        pool.most_recent_latency_ms !== undefined
    )
        items.push({ label: 'last latency', value: `${pool.most_recent_latency_ms} ms` });

    if (items.length === 0) return null;
    return (
        <dl className="grid grid-cols-3 gap-2 text-[11px]">
            {items.map(it => (
                <div
                    key={it.label}
                    className="bg-surface-muted rounded px-2 py-1"
                >
                    <dt className="text-tertiary uppercase tracking-wide">
                        {it.label}
                    </dt>
                    <dd className="text-default font-medium">{it.value}</dd>
                </div>
            ))}
        </dl>
    );
};

// ─── Visual helpers ──────────────────────────────────────────────

function kindIcon(kind: string): React.ReactNode {
    if (kind === 'postgres') return <Database className="w-4 h-4 text-secondary" />;
    if (kind === 'redis' || kind === 'qdrant')
        return <Server className="w-4 h-4 text-secondary" />;
    if (kind.startsWith('llm_provider:'))
        return <Brain className="w-4 h-4 text-secondary" />;
    return <Server className="w-4 h-4 text-secondary" />;
}

function formatTimestamp(iso: string | null): string {
    if (!iso) return 'never';
    try {
        return new Date(iso).toLocaleString();
    } catch {
        return iso;
    }
}

interface GroupedPools {
    infra: ConnectionPool[];
    llm: ConnectionPool[];
}

function groupPools(pools: ConnectionPool[]): GroupedPools {
    const infra: ConnectionPool[] = [];
    const llm: ConnectionPool[] = [];
    for (const p of pools) {
        if (p.kind.startsWith('llm_provider:')) {
            llm.push(p);
        } else {
            infra.push(p);
        }
    }
    return { infra, llm };
}

export default SystemConnectionsPane;

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
    CheckCircle2,
    AlertTriangle,
    XCircle,
    HelpCircle,
    MinusCircle,
    Database,
    Server,
    Brain,
    Plug,
    RefreshCw,
    Wrench,
} from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import {
    DependencyKind,
    HealthCard,
    HealthStatus,
    OverviewCounters,
    OverviewMaintenance,
    SystemOverviewResponse,
} from '../../lib/systemOpsTypes';
import { errorMessage } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';

/**
 * SystemOverviewPane.
 *
 * At-a-glance health grid. Polls the backend on a 15s interval so
 * operators can keep the tab open during an incident without spamming
 * the API. A manual Refresh button piggybacks on the same fetch but
 * surfaces error states immediately.
 *
 * Card grouping: cards are bucketed by `kind` and rendered as
 * 6 sections (infra / LLM / MCP / others). The empty buckets
 * collapse to a "No upstreams configured" hint rather than rendering
 * a blank box — operators get an explicit answer instead of being
 * left wondering whether the API returned data.
 *
 * Auto-refresh pause: when the tab isn't visible (per the Page
 * Visibility API) we skip the next poll. Saves cost for users who
 * leave the tab open in a background window.
 */

const POLL_INTERVAL_MS = 15_000;

interface Props {
    /** Re-dispatch into another sub-section. Cards deep-link via target_route. */
    onTabChange?: (tab: string) => void;
}

const SystemOverviewPane: React.FC<Props> = ({ onTabChange }) => {
    const { gatewayClient } = useGateway();
    const [data, setData] = useState<SystemOverviewResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [lastFetchedAt, setLastFetchedAt] = useState<Date | null>(null);

    const fetchOverview = useCallback(async () => {
        try {
            const resp = await gatewayClient.getSystemOverview();
            setData(resp);
            setError(null);
            setLastFetchedAt(new Date());
        } catch (e) {
            setError(errorMessage(e) || String(e));
        } finally {
            setLoading(false);
        }
    }, [gatewayClient]);

    // Initial load + polling. Visibility check avoids pointless
    // polls when the tab is in a background window.
    //
    // We deliberately omit `fetchOverview` from the dep array — its
    // identity changes on every parent render because `gatewayClient`
    // from useGateway() is a fresh object literal each call. Including
    // it would cause an infinite re-fetch loop in tests (and a noisy
    // hot-reload churn in dev). The polling interval picks up the
    // latest closure via the next tick.
     
    useEffect(() => {
        runBackground(fetchOverview(), 'overview');
        const id = setInterval(() => {
            if (document.visibilityState === 'visible') {
                runBackground(fetchOverview(), 'overview');
            }
        }, POLL_INTERVAL_MS);
        return () => clearInterval(id);
    }, []);

    const grouped = useMemo(() => groupCards(data?.cards ?? []), [data]);

    if (loading && !data) {
        return (
            <div className="text-secondary text-sm">Loading overview…</div>
        );
    }

    if (error && !data) {
        return (
            <div
                data-testid="system-overview-error"
                className="bg-red-50 border border-red-200 text-red-800 p-4 rounded-md max-w-2xl"
            >
                <div className="font-semibold">Couldn't load overview</div>
                <div className="text-sm mt-1">{error}</div>
                <button
                    type="button"
                    className="mt-3 px-3 py-1.5 text-sm rounded border border-red-300 hover:bg-red-100"
                    onClick={asyncHandler(fetchOverview, 'click')}
                >
                    Retry
                </button>
            </div>
        );
    }

    if (!data) return null;

    return (
        <div className="space-y-6 max-w-6xl">
            {/* ── Header strip: counters + maintenance + refresh ── */}
            <header className="flex flex-wrap items-start gap-4 justify-between">
                <CountersStrip counters={data.counters} />
                <div className="flex items-center gap-3">
                    <MaintenanceBadge maintenance={data.maintenance} />
                    <button
                        type="button"
                        onClick={asyncHandler(fetchOverview, 'click')}
                        data-testid="system-overview-refresh"
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
                </div>
            </header>

            {/* ── Card grid, grouped by kind ── */}
            <CardGroup
                title="Infrastructure"
                description="Postgres, Redis, Qdrant — the dependencies every gateway replica needs to serve a request."
                cards={grouped.infra}
                emptyHint="Probes will appear once configured."
                onTabChange={onTabChange}
            />
            <CardGroup
                title="LLM Upstreams"
                description="One card per enabled LLM model. Status reflects failures recorded by the proxy in the last 5 minutes."
                cards={grouped.llm}
                emptyHint="No LLM models enabled for this tenant."
                onTabChange={onTabChange}
            />
            <CardGroup
                title="MCP Servers"
                description="One card per registered MCP server. Disabled servers render greyed; live failures appear as down/degraded."
                cards={grouped.mcp}
                emptyHint="No MCP servers registered for this tenant."
                onTabChange={onTabChange}
            />

            {/* Last-checked footer — operators care about staleness. */}
            <footer className="text-xs text-tertiary pt-2 border-t border-default">
                Generated {formatTimestamp(data.generated_at)}
                {lastFetchedAt && (
                    <> · client refreshed {lastFetchedAt.toLocaleTimeString()}</>
                )}
            </footer>
        </div>
    );
};

// ─── Counters strip ──────────────────────────────────────────────

const CountersStrip: React.FC<{ counters: OverviewCounters }> = ({
    counters,
}) => {
    const items: { label: string; value: number; status: HealthStatus }[] = [
        { label: 'Healthy', value: counters.healthy, status: 'healthy' },
        { label: 'Degraded', value: counters.degraded, status: 'degraded' },
        { label: 'Down', value: counters.down, status: 'down' },
        { label: 'Unknown', value: counters.unknown, status: 'unknown' },
        { label: 'Disabled', value: counters.disabled, status: 'disabled' },
    ];
    return (
        <div
            data-testid="system-overview-counters"
            className="flex items-center gap-3 flex-wrap"
        >
            {items.map(it => (
                <div
                    key={it.label}
                    className={`flex items-center gap-2 px-3 py-1.5 rounded-md border ${statusBorderClass(it.status)}`}
                >
                    {statusIcon(it.status)}
                    <span className="text-sm">
                        <span className="font-semibold">{it.value}</span>{' '}
                        <span className="text-secondary">{it.label}</span>
                    </span>
                </div>
            ))}
        </div>
    );
};

// ─── Maintenance badge ───────────────────────────────────────────

const MaintenanceBadge: React.FC<{ maintenance: OverviewMaintenance }> = ({
    maintenance,
}) => {
    if (maintenance.mode === 'off') return null;
    return (
        <div
            data-testid="system-overview-maintenance-banner"
            className="inline-flex items-center gap-2 px-3 py-1.5 rounded-md bg-amber-100 dark:bg-amber-900/30 text-amber-900 dark:text-amber-200 border border-amber-300 dark:border-amber-700"
        >
            <Wrench className="w-4 h-4" />
            <span className="text-sm font-medium">
                Maintenance: {maintenance.mode}
            </span>
            {maintenance.since && (
                <span className="text-xs">
                    since {formatTimestamp(maintenance.since)}
                </span>
            )}
        </div>
    );
};

// ─── Card group ──────────────────────────────────────────────────

interface CardGroupProps {
    title: string;
    description: string;
    cards: HealthCard[];
    emptyHint: string;
    onTabChange?: (tab: string) => void;
}

const CardGroup: React.FC<CardGroupProps> = ({
    title,
    description,
    cards,
    emptyHint,
    onTabChange,
}) => (
    <section>
        <div className="mb-3">
            <h2 className="text-base font-semibold text-default">{title}</h2>
            <p className="text-xs text-secondary">{description}</p>
        </div>
        {cards.length === 0 ? (
            <div
                data-testid={`system-overview-group-empty-${title.toLowerCase().replace(/\s+/g, '-')}`}
                className="text-sm text-tertiary italic"
            >
                {emptyHint}
            </div>
        ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {cards.map(c => (
                    <HealthCardView
                        key={`${c.kind}:${c.id}`}
                        card={c}
                        onClick={
                            c.target_route && onTabChange
                                ? () => onTabChange(c.target_route!)
                                : undefined
                        }
                    />
                ))}
            </div>
        )}
    </section>
);

// ─── Card view ───────────────────────────────────────────────────

const HealthCardView: React.FC<{ card: HealthCard; onClick?: () => void }> = ({
    card,
    onClick,
}) => {
    const interactive = !!onClick;
    return (
        <button
            type="button"
            onClick={onClick}
            disabled={!interactive}
            data-testid={`system-overview-card-${card.kind}-${card.id}`}
            className={`text-left p-3 rounded-md border ${statusBorderClass(card.status)} bg-surface ${
                interactive
                    ? 'hover:bg-surface-muted cursor-pointer'
                    : 'cursor-default'
            }`}
        >
            <div className="flex items-start gap-2">
                <div className="flex-shrink-0 mt-0.5">
                    {kindIcon(card.kind)}
                </div>
                <div className="min-w-0 flex-1 min-h-0">
                    <div className="flex items-center justify-between gap-2">
                        <span className="text-sm font-medium text-default truncate">
                            {card.label}
                        </span>
                        {statusIcon(card.status)}
                    </div>
                    {card.detail && (
                        <div className="text-xs text-secondary mt-1 break-words">
                            {card.detail}
                        </div>
                    )}
                    {card.latency_ms !== null && (
                        <div className="text-[11px] text-tertiary mt-1">
                            {card.latency_ms} ms
                        </div>
                    )}
                </div>
            </div>
        </button>
    );
};

// ─── Visual helpers ──────────────────────────────────────────────

function statusIcon(status: HealthStatus): React.ReactNode {
    switch (status) {
        case 'healthy':
            return <CheckCircle2 className="w-4 h-4 text-emerald-600" />;
        case 'degraded':
            return <AlertTriangle className="w-4 h-4 text-amber-600" />;
        case 'down':
            return <XCircle className="w-4 h-4 text-red-600" />;
        case 'unknown':
            return <HelpCircle className="w-4 h-4 text-slate-500" />;
        case 'disabled':
            return <MinusCircle className="w-4 h-4 text-slate-400" />;
    }
}

function statusBorderClass(status: HealthStatus): string {
    switch (status) {
        case 'healthy':
            return 'border-emerald-200 dark:border-emerald-800';
        case 'degraded':
            return 'border-amber-200 dark:border-amber-800';
        case 'down':
            return 'border-red-200 dark:border-red-800';
        case 'unknown':
            return 'border-slate-200 dark:border-slate-700';
        case 'disabled':
            return 'border-slate-100 dark:border-slate-800 opacity-60';
    }
}

function kindIcon(kind: DependencyKind): React.ReactNode {
    switch (kind) {
        case 'database':
            return <Database className="w-4 h-4 text-secondary" />;
        case 'redis':
            return <Server className="w-4 h-4 text-secondary" />;
        case 'qdrant':
            return <Server className="w-4 h-4 text-secondary" />;
        case 'llm_upstream':
            return <Brain className="w-4 h-4 text-secondary" />;
        case 'mcp_server':
            return <Plug className="w-4 h-4 text-secondary" />;
        default:
            return <Server className="w-4 h-4 text-secondary" />;
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

interface GroupedCards {
    infra: HealthCard[];
    llm: HealthCard[];
    mcp: HealthCard[];
}

function groupCards(cards: HealthCard[]): GroupedCards {
    const infra: HealthCard[] = [];
    const llm: HealthCard[] = [];
    const mcp: HealthCard[] = [];
    for (const c of cards) {
        if (
            c.kind === 'database' ||
            c.kind === 'redis' ||
            c.kind === 'qdrant'
        ) {
            infra.push(c);
        } else if (c.kind === 'llm_upstream') {
            llm.push(c);
        } else if (c.kind === 'mcp_server') {
            mcp.push(c);
        } else {
            // Future kinds (skill_runner, kb_connector_sync, registry,
            // gateway_core) fall back to infra section until they get
            // their own bucket. Better than dropping silently.
            infra.push(c);
        }
    }
    return { infra, llm, mcp };
}

export default SystemOverviewPane;

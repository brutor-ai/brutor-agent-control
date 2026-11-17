/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
    Gauge,
    Lock,
    RefreshCw,
    RotateCcw,
    Snowflake,
    X,
} from 'lucide-react';

import { useAuth } from '../../hooks/useAuth';
import { useGateway } from '../../hooks/useGateway';
import { PERM_OPERATE } from './systemPermissions';
import { TABLE_TH_TYPE, TABLE_THEAD } from '../ui/tableStyles';
import {
    CounterEntry,
    CounterKind,
    CounterListResponse,
    CounterScope,
} from '../../lib/systemOpsTypes';
import {
    readFiltersFromUrl,
    useUrlFilterSync,
} from '../monitoring/useUrlFilterState';
import { errorMessage } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';

/**
 * SystemCountersPane.
 *
 * Live view + operator controls for the rate-limit / quota counters
 * the Rust proxy enforces. Authoritative state lives inside
 * `UsageState` on every gateway-core pod; the control plane reads a
 * best-effort approximation from `usage_logs` for the last 60s window. The pane is consequently honest about that approximation
 * — the drawer text below the table makes it explicit — so an operator
 * doesn't mistake a transient zero for "the proxy is idle".
 *
 * Key behaviours:
 *
 *   1. Visibility-gated 10s poll. Mirrors `SystemOverviewPane` —
 *      the background-tab case is one of the worst no-cost paths to
 *      leave hammering the API.
 *
 *   2. Filters: group / kind / only-cooling-down. All client-side
 *      against the same fetched list — the API also accepts these as
 *      query params, but the page is small enough that re-fetching on
 *      every keystroke is more disruptive than re-rendering the
 *      filter view.
 *
 *   3. Per-row actions: Reset, Release Cooldown. Both gated on
 *      `system:operate`. The same disabled-tooltip pattern the
 *      placeholder uses is preserved so the role-gating UX is
 *      consistent across the console.
 *
 *   4. Confirm modal blocks an accidental fire. Counter resets are
 *      effectively a privileged "let the next minute of traffic
 *      through" — recoverable but operator-visible. A double-click
 *      on the wrong row should never quietly execute.
 *
 *   5. Detail drawer: click a row to see the raw payload
 *      and a note about authoritative state. We intentionally don't
 *      yet show audit-log entries inline — that's the Audit tab, and duplicating its filter envelope here would
 *      drift it.
 */

const POLL_INTERVAL_MS = 10_000;

interface Props {
    /** Re-dispatch into another sub-section if a drawer link wants
     *  to take the operator to e.g. the Audit tab for matching rows.
     *  Kept symmetric with the Overview pane signature. */
    onTabChange?: (tab: string) => void;
}

const SystemCountersPane: React.FC<Props> = ({ onTabChange: _onTabChange }) => {
    const { gatewayClient } = useGateway();
    const { hasPermission } = useAuth();
    const canOperate = hasPermission(PERM_OPERATE);

    // ── Data + load lifecycle ────────────────────────────────────
    const [data, setData] = useState<CounterListResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [lastFetchedAt, setLastFetchedAt] = useState<Date | null>(null);

    // ── Filter UI state ──────────────────────────────────────────
    //
    // Initialised from the URL when the operator landed here via a
    // deep link (PagerDuty alert, Proxy Logs row, Mission Control
    // alert detail). Empty defaults otherwise. The hydration runs
    // synchronously inside `useState`'s initialiser so the first
    // render already has the correct filters — otherwise the
    // initial fetch would fire with no scope and the
    // drawer-auto-open effect would race against the first
    // setState.
    const initialUrlFilters = useMemo(() => {
        return readFiltersFromUrl<{
            scope: CounterScope | '';
            subject: string;
            kind: string;
            group: string;
            drawer: string;
        }>('cnt-', {
            scope: (raw) => {
                const allowed: ReadonlyArray<CounterScope> = [
                    'tenant', 'group', 'user', 'api_key',
                    'model', 'mcp_server',
                ];
                return raw && (allowed as readonly string[]).includes(raw)
                    ? (raw as CounterScope)
                    : '';
            },
            subject: (raw) => raw ?? '',
            kind: (raw) => raw ?? '',
            group: (raw) => raw ?? '',
            drawer: (raw) => (raw === 'open' ? 'open' : ''),
        });
         
    }, []);

    const [scopeFilter, setScopeFilter] = useState<CounterScope | ''>(
        initialUrlFilters?.scope ?? '',
    );
    const [subjectFilter, setSubjectFilter] = useState<string>(
        initialUrlFilters?.subject ?? '',
    );
    const [groupFilter, setGroupFilter] = useState<string>(
        initialUrlFilters?.group ?? '',
    );
    const [kindFilter, setKindFilter] = useState<string>(
        initialUrlFilters?.kind ?? '',
    );
    const [onlyCoolingDown, setOnlyCoolingDown] = useState(false);

    // ── Drawer + confirm modal state ─────────────────────────────
    const [selected, setSelected] = useState<CounterEntry | null>(null);
    const [drawerAutoOpenPending, setDrawerAutoOpenPending] = useState<boolean>(
        initialUrlFilters?.drawer === 'open',
    );
    const [confirm, setConfirm] = useState<ConfirmIntent | null>(null);
    const [actionInFlight, setActionInFlight] = useState(false);

    // Deep-link contract — keep the URL in sync as the
    // operator narrows their filters. The next bookmark / share gets
    // the same view. Empty strings serialise as null so the URL
    // stays short for the common case where no filter is set.
    useUrlFilterSync(
        'cnt-',
        {
            scope: scopeFilter,
            subject: subjectFilter,
            kind: kindFilter,
            group: groupFilter,
            drawer: selected ? 'open' : '',
        },
        {
            scope: (v) => (v ? v : null),
            subject: (v) => (v ? v : null),
            kind: (v) => (v ? v : null),
            group: (v) => (v ? v : null),
            drawer: (v) => (v ? v : null),
        },
    );

    const fetchCounters = useCallback(async () => {
        try {
            // Polled fetch: empty filter. Client-side filtering on
            // top — tearing the row order during a tick would be
            // worse than re-filtering in JS.
            //
            // Exception: when a deep link pinned `scope`, pass it
            // through. Suppressing the off-scope family backend-side
            // makes the empty-state copy accurate ("no rows for
            // scope=mcp_server") rather than rendering a list of LLM
            // counters the operator's alert wasn't about.
            const resp = await gatewayClient.listSystemCounters(
                scopeFilter ? { scope: scopeFilter } : {},
            );
            setData(resp);
            setError(null);
            setLastFetchedAt(new Date());
        } catch (e) {
            setError(errorMessage(e) || String(e));
        } finally {
            setLoading(false);
        }
    }, [gatewayClient, scopeFilter]);

    // Initial load + polling. `fetchCounters` is intentionally NOT in
    // the dep array — `gatewayClient` from useGateway() is a fresh
    // object literal each render, so including the callback would loop
    // (and break test mocks that use mockResolvedValueOnce).
     
    useEffect(() => {
        runBackground(fetchCounters(), 'counters');
        const id = setInterval(() => {
            // Page Visibility API gate — same rationale as the
            // Overview pane: keep cost flat for backgrounded tabs.
            if (document.visibilityState === 'visible') {
                runBackground(fetchCounters(), 'counters');
            }
        }, POLL_INTERVAL_MS);
        return () => clearInterval(id);
    }, []);

    // ── Derived: filtered counter rows ───────────────────────────
    const rows = useMemo(() => {
        const all = data?.counters ?? [];
        return all.filter(c => {
            if (groupFilter && c.group_id !== groupFilter) return false;
            if (kindFilter && c.kind !== kindFilter) return false;
            // Subject filter — pinned by a deep link's `?cnt-subject=`.
            // Matches the row's subject_id verbatim. Empty = no
            // filter so the operator can clear the chip and see the
            // full workspace.
            if (subjectFilter && c.subject_id !== subjectFilter) return false;
            if (onlyCoolingDown && !c.cooldown_active) return false;
            return true;
        });
    }, [data, groupFilter, kindFilter, subjectFilter, onlyCoolingDown]);

    // Deep-link drawer auto-open: when the operator arrived with
    // ?cnt-drawer=open AND exactly one row matches their pinned
    // scope/subject/kind, open the detail drawer automatically so
    // they land mid-incident with the row they came to see already
    // expanded. Run-once: `drawerAutoOpenPending` is cleared on
    // first match (or when rows arrive but no match — operator can
    // still open it manually). Multiple matches: leave it to the
    // operator to click, otherwise we'd pick an arbitrary row.
    useEffect(() => {
        if (!drawerAutoOpenPending) return;
        if (!data) return; // wait for first fetch
        const matches = rows;
        if (matches.length === 1) {
            setSelected(matches[0]);
            setDrawerAutoOpenPending(false);
        } else if (matches.length === 0) {
            // No row to open onto — clear the pending flag so the
            // operator's next manual click sticks.
            setDrawerAutoOpenPending(false);
        }
        // matches.length > 1: leave pending so a refined filter
        // could still trigger auto-open.
    }, [drawerAutoOpenPending, data, rows]);

    // Group list for the dropdown — distinct group_ids surfaced by
    // the current fetch. Avoids a second round-trip for group
    // metadata; if the operator wants a friendlier label the
    // group name lives on `subject_label`, but that's the
    // bucket-subject label, not the group label, so we leave the
    // id as the disambiguator.
    const groupOptions = useMemo(() => {
        const set = new Set<string>();
        for (const c of data?.counters ?? []) {
            if (c.group_id) set.add(c.group_id);
        }
        return Array.from(set).sort();
    }, [data]);

    // ── Confirm + action handlers ────────────────────────────────

    const requestReset = (row: CounterEntry) => {
        if (!canOperate) return;
        setConfirm({ kind: 'reset', row });
    };
    const requestRelease = (row: CounterEntry) => {
        if (!canOperate) return;
        setConfirm({ kind: 'release', row });
    };

    const executeConfirmed = useCallback(async () => {
        if (!confirm) return;
        setActionInFlight(true);
        try {
            if (confirm.kind === 'reset') {
                await gatewayClient.resetSystemCounter({
                    scope: confirm.row.group_id ? 'group' : 'tenant',
                    id: confirm.row.group_id ?? confirm.row.tenant_id,
                    limit_types: [confirm.row.kind],
                });
            } else {
                await gatewayClient.releaseSystemCooldown({
                    scope: confirm.row.group_id ? 'group' : 'tenant',
                    id: confirm.row.group_id ?? confirm.row.tenant_id,
                });
            }
            setConfirm(null);
            // Optimistic re-poll so the operator sees the effect
            // immediately rather than waiting for the 10s tick.
            runBackground(fetchCounters(), 'counters');
        } catch (e) {
            // Surface the error inline rather than as a toast —
            // operator actions are high-stakes, and a toast that's
            // already faded is a worse failure mode than a banner
            // on the confirm modal.
            setError(errorMessage(e) || String(e));
        } finally {
            setActionInFlight(false);
        }
    }, [confirm, gatewayClient, fetchCounters]);

    // ── Render branches ──────────────────────────────────────────

    if (loading && !data) {
        return <div className="text-secondary text-sm">Loading counters…</div>;
    }

    if (error && !data) {
        return (
            <div
                data-testid="system-counters-error"
                className="bg-red-50 border border-red-200 text-red-800 p-4 rounded-md max-w-2xl"
            >
                <div className="font-semibold">Couldn't load counters</div>
                <div className="text-sm mt-1">{error}</div>
                <button
                    type="button"
                    className="mt-3 px-3 py-1.5 text-sm rounded border border-red-300 hover:bg-red-100"
                    onClick={asyncHandler(fetchCounters, 'click')}
                >
                    Retry
                </button>
            </div>
        );
    }

    if (!data) return null;

    return (
        <div className="space-y-4 max-w-6xl" data-testid="system-counters-pane">
            {/* ── Header strip: counters + refresh ── */}
            <header className="flex flex-wrap items-center gap-3 justify-between">
                <div className="flex items-center gap-2 text-default">
                    <Gauge className="w-5 h-5 text-primary" />
                    <h2 className="text-base font-semibold">Counters</h2>
                    <span className="text-xs text-tertiary">
                        ({rows.length} of {data.counters.length} shown)
                    </span>
                </div>
                <button
                    type="button"
                    onClick={asyncHandler(fetchCounters, 'click')}
                    data-testid="system-counters-refresh"
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

            {/* ── Filters ── */}
            <FilterBar
                groupOptions={groupOptions}
                groupFilter={groupFilter}
                onGroupFilterChange={setGroupFilter}
                kindFilter={kindFilter}
                onKindFilterChange={setKindFilter}
                onlyCoolingDown={onlyCoolingDown}
                onOnlyCoolingDownChange={setOnlyCoolingDown}
            />

            {/* ── Deep-link context chips ──
                When the operator arrives via PagerDuty / Proxy Logs /
                Mission Control alert with a pinned scope or subject,
                surface those constraints as dismissable chips so it's
                obvious why the row count is narrow. Each chip clears
                its own filter on click, which also clears its URL
                param via useUrlFilterSync. */}
            {(scopeFilter || subjectFilter) && (
                <div
                    data-testid="system-counters-deeplink-chips"
                    className="flex flex-wrap items-center gap-2 text-xs"
                >
                    <span className="text-tertiary">
                        Arrived from a deep link:
                    </span>
                    {scopeFilter && (
                        <button
                            type="button"
                            data-testid="system-counters-chip-scope"
                            onClick={() => setScopeFilter('')}
                            className="inline-flex items-center gap-1 bg-surface-muted border border-default rounded-full px-2 py-0.5 hover:border-primary"
                            title="Click to clear"
                        >
                            scope = <strong className="font-mono">{scopeFilter}</strong>
                            <X className="w-3 h-3" />
                        </button>
                    )}
                    {subjectFilter && (
                        <button
                            type="button"
                            data-testid="system-counters-chip-subject"
                            onClick={() => setSubjectFilter('')}
                            className="inline-flex items-center gap-1 bg-surface-muted border border-default rounded-full px-2 py-0.5 hover:border-primary"
                            title="Click to clear"
                        >
                            subject = <strong className="font-mono">{subjectFilter}</strong>
                            <X className="w-3 h-3" />
                        </button>
                    )}
                </div>
            )}

            {/* ── How counters work ── */}
            <div
                data-testid="system-counters-approximation-banner"
                className="text-xs text-tertiary bg-surface-muted rounded-md px-3 py-3 border border-default space-y-2"
            >
                <div>
                    <span className="text-secondary font-medium">
                        What you&rsquo;re looking at.
                    </span>{' '}
                    Each row is one rate-limit bucket the gateway is currently enforcing &mdash; e.g. requests-per-minute
                    for a specific LLM model, or per-tool MCP calls for a
                    resource group. <strong>Used</strong> is how much of the
                    bucket has been consumed in the rolling window;{' '}
                    <strong>Limit</strong> is what the proxy will reject at.
                </div>
                <div>
                    <span className="text-secondary font-medium">
                        Why a row might disappear.
                    </span>{' '}
                    Usage is read from{' '}
                    <code className="bg-surface-default px-1 rounded">
                        usage_logs
                    </code>{' '}
                    over the last <strong>60 seconds</strong>. If no traffic
                    has hit a bucket in that window, its row drops off until
                    the next request lands &mdash; the proxy is still
                    enforcing the limit, you&rsquo;re just not seeing
                    consumption against it. The page is a live snapshot, not
                    a per-minute / per-hour history (proxy logs serves that).
                </div>
                <div>
                    <span className="text-secondary font-medium">
                        Reset / Release.
                    </span>{' '}
                    These are <em>authoritative</em> &mdash; they
                    reach every gateway replica in milliseconds. Use Reset to clear a stuck
                    counter, Release to lift an active cooldown.
                </div>
            </div>

            {/* ── Table ── */}
            <CountersTable
                rows={rows}
                canOperate={canOperate}
                onSelect={setSelected}
                onReset={requestReset}
                onRelease={requestRelease}
            />

            {/* Footer */}
            <footer className="text-xs text-tertiary pt-2 border-t border-default">
                Generated {formatTimestamp(data.generated_at)}
                {lastFetchedAt && (
                    <> · client refreshed {lastFetchedAt.toLocaleTimeString()}</>
                )}
            </footer>

            {/* Detail drawer */}
            {selected && (
                <DetailDrawer
                    row={selected}
                    onClose={() => setSelected(null)}
                />
            )}

            {/* Confirm modal */}
            {confirm && (
                <ConfirmModal
                    intent={confirm}
                    inFlight={actionInFlight}
                    error={error}
                    onCancel={() => {
                        if (actionInFlight) return;
                        setConfirm(null);
                        setError(null);
                    }}
                    onConfirm={asyncHandler(executeConfirmed, 'confirm')}
                />
            )}
        </div>
    );
};

// ─── Filter bar ──────────────────────────────────────────────────

interface FilterBarProps {
    groupOptions: string[];
    groupFilter: string;
    onGroupFilterChange: (v: string) => void;
    kindFilter: string;
    onKindFilterChange: (v: string) => void;
    onlyCoolingDown: boolean;
    onOnlyCoolingDownChange: (v: boolean) => void;
}

const KIND_OPTIONS: CounterKind[] = [
    'llm_request_rate',
    'llm_input_tokens',
    'llm_output_tokens',
    'llm_concurrent',
    'llm_spend',
    'mcp_tool_rate',
    'mcp_session_rate',
];

const FilterBar: React.FC<FilterBarProps> = ({
    groupOptions,
    groupFilter,
    onGroupFilterChange,
    kindFilter,
    onKindFilterChange,
    onlyCoolingDown,
    onOnlyCoolingDownChange,
}) => (
    <div
        data-testid="system-counters-filter-bar"
        className="flex flex-wrap items-center gap-3"
    >
        <label className="flex items-center gap-2 text-sm">
            <span className="text-secondary">Group:</span>
            <select
                data-testid="system-counters-filter-group"
                value={groupFilter}
                onChange={e => onGroupFilterChange(e.target.value)}
                className="text-sm bg-surface border border-default rounded px-2 py-1"
            >
                <option value="">All</option>
                {groupOptions.map(g => (
                    <option key={g} value={g}>
                        {g}
                    </option>
                ))}
            </select>
        </label>
        <label className="flex items-center gap-2 text-sm">
            <span className="text-secondary">Kind:</span>
            <select
                data-testid="system-counters-filter-kind"
                value={kindFilter}
                onChange={e => onKindFilterChange(e.target.value)}
                className="text-sm bg-surface border border-default rounded px-2 py-1"
            >
                <option value="">All</option>
                {KIND_OPTIONS.map(k => (
                    <option key={k} value={k}>
                        {k}
                    </option>
                ))}
            </select>
        </label>
        <label className="flex items-center gap-2 text-sm">
            <input
                data-testid="system-counters-filter-cooling"
                type="checkbox"
                checked={onlyCoolingDown}
                onChange={e => onOnlyCoolingDownChange(e.target.checked)}
            />
            <span className="text-secondary">Only cooling down</span>
        </label>
    </div>
);

// ─── Counters table ──────────────────────────────────────────────

interface CountersTableProps {
    rows: CounterEntry[];
    canOperate: boolean;
    onSelect: (row: CounterEntry) => void;
    onReset: (row: CounterEntry) => void;
    onRelease: (row: CounterEntry) => void;
}

const CountersTable: React.FC<CountersTableProps> = ({
    rows,
    canOperate,
    onSelect,
    onReset,
    onRelease,
}) => {
    if (rows.length === 0) {
        return (
            <div
                data-testid="system-counters-empty"
                className="text-sm text-tertiary space-y-2"
            >
                <p className="italic">
                    No active counters in the last 60 seconds.
                </p>
                <p className="not-italic text-xs">
                    This is normal when the proxy is idle &mdash; rows only
                    appear here when there&rsquo;s recent traffic against a
                    rate-limited subject (LLM model, resource group, API key,
                    MCP tool). Send a few requests through the proxy and they
                    will populate within seconds. Clear any active filters
                    above if you expected matches that aren&rsquo;t showing.
                </p>
            </div>
        );
    }
    return (
        <div className="overflow-x-auto">
            <table className="min-w-full text-sm">
                <thead className={TABLE_THEAD}>
                    <tr className="text-left">
                        <th className={`py-2 pr-4 ${TABLE_TH_TYPE}`}>Subject</th>
                        <th className={`py-2 pr-4 ${TABLE_TH_TYPE}`}>Kind</th>
                        <th className={`py-2 pr-4 ${TABLE_TH_TYPE}`}>Used / Limit</th>
                        <th className={`py-2 pr-4 ${TABLE_TH_TYPE}`}>Window</th>
                        <th className={`py-2 pr-4 ${TABLE_TH_TYPE}`}>Cooldown</th>
                        <th className={`py-2 pr-4 ${TABLE_TH_TYPE}`}>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {rows.map(row => (
                        <CounterRowView
                            key={`${row.group_id ?? '_'}:${row.subject_id ?? '_'}:${row.kind}`}
                            row={row}
                            canOperate={canOperate}
                            onSelect={() => onSelect(row)}
                            onReset={() => onReset(row)}
                            onRelease={() => onRelease(row)}
                        />
                    ))}
                </tbody>
            </table>
        </div>
    );
};

interface CounterRowViewProps {
    row: CounterEntry;
    canOperate: boolean;
    onSelect: () => void;
    onReset: () => void;
    onRelease: () => void;
}

const CounterRowView: React.FC<CounterRowViewProps> = ({
    row,
    canOperate,
    onSelect,
    onReset,
    onRelease,
}) => {
    const pct =
        row.limit > 0 ? Math.min(100, Math.round((row.used / row.limit) * 100)) : 0;
    const barColor =
        pct >= 90
            ? 'bg-red-500'
            : pct >= 75
              ? 'bg-amber-500'
              : 'bg-emerald-500';

    return (
        <tr
            data-testid={`system-counters-row-${row.kind}-${row.subject_id ?? 'all'}`}
            className="border-b border-default hover:bg-surface-muted cursor-pointer"
            onClick={onSelect}
        >
            <td className="py-2 pr-4">
                <div className="font-medium text-default">
                    {row.subject_label ?? row.subject_id ?? '(tenant-wide)'}
                </div>
                {row.group_id && (
                    <div className="text-xs text-tertiary">
                        group: {row.group_id}
                    </div>
                )}
            </td>
            <td className="py-2 pr-4">
                <code className="text-xs bg-surface-muted px-1.5 py-0.5 rounded">
                    {row.kind}
                </code>
            </td>
            <td className="py-2 pr-4">
                <div className="flex items-center gap-2">
                    <span className="font-mono text-xs">
                        {row.used} / {row.limit > 0 ? row.limit : '∞'}
                    </span>
                    {row.limit > 0 && (
                        <div className="w-24 h-1.5 bg-surface-muted rounded">
                            <div
                                data-testid={`system-counters-bar-${row.kind}-${row.subject_id ?? 'all'}`}
                                className={`h-1.5 rounded ${barColor}`}
                                style={{ width: `${pct}%` }}
                            />
                        </div>
                    )}
                </div>
            </td>
            <td className="py-2 pr-4 text-xs text-secondary">{row.window}</td>
            <td className="py-2 pr-4">
                {row.cooldown_active ? (
                    <span
                        data-testid={`system-counters-cooldown-badge-${row.kind}-${row.subject_id ?? 'all'}`}
                        className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-amber-100 dark:bg-amber-900/30 text-amber-800 dark:text-amber-200"
                    >
                        <Snowflake className="w-3 h-3" />
                        Cooling
                    </span>
                ) : (
                    <span className="text-xs text-tertiary">—</span>
                )}
            </td>
            <td className="py-2 pr-4">
                <div
                    className="flex items-center gap-2"
                    // Stop click bubbling so action buttons don't
                    // also open the detail drawer underneath them.
                    onClick={e => e.stopPropagation()}
                >
                    <ActionButton
                        label="Reset"
                        icon={<RotateCcw className="w-3.5 h-3.5" />}
                        canOperate={canOperate}
                        onClick={onReset}
                        testId={`system-counters-reset-${row.kind}-${row.subject_id ?? 'all'}`}
                    />
                    <ActionButton
                        label="Release"
                        icon={<Snowflake className="w-3.5 h-3.5" />}
                        canOperate={canOperate}
                        onClick={onRelease}
                        testId={`system-counters-release-${row.kind}-${row.subject_id ?? 'all'}`}
                    />
                </div>
            </td>
        </tr>
    );
};

interface ActionButtonProps {
    label: string;
    icon: React.ReactNode;
    canOperate: boolean;
    onClick: () => void;
    testId: string;
}

const ActionButton: React.FC<ActionButtonProps> = ({
    label,
    icon,
    canOperate,
    onClick,
    testId,
}) => (
    <button
        type="button"
        data-testid={testId}
        disabled={!canOperate}
        onClick={onClick}
        title={
            canOperate
                ? label
                : `Requires the system:operate permission`
        }
        className={`inline-flex items-center gap-1 px-2 py-1 rounded border text-xs ${
            canOperate
                ? 'border-default text-default hover:bg-surface-muted'
                : 'border-default text-tertiary opacity-50 cursor-not-allowed'
        }`}
    >
        {!canOperate && <Lock className="w-3 h-3" />}
        {icon}
        {label}
    </button>
);

// ─── Detail drawer ───────────────────────────────────────────────

interface DetailDrawerProps {
    row: CounterEntry;
    onClose: () => void;
}

const DetailDrawer: React.FC<DetailDrawerProps> = ({ row, onClose }) => (
    <div
        data-testid="system-counters-drawer"
        className="fixed inset-y-0 right-0 w-full sm:max-w-md bg-surface border-l border-default shadow-xl z-40 overflow-y-auto"
    >
        <div className="flex items-center justify-between p-4 border-b border-default">
            <div className="flex items-center gap-2">
                <Gauge className="w-5 h-5 text-primary" />
                <h3 className="text-sm font-semibold">{row.kind}</h3>
            </div>
            <button
                type="button"
                onClick={onClose}
                data-testid="system-counters-drawer-close"
                className="p-1 hover:bg-surface-muted rounded"
            >
                <X className="w-4 h-4" />
            </button>
        </div>
        <div className="p-4 space-y-3 text-sm">
            <FieldRow label="Subject" value={row.subject_label ?? row.subject_id ?? '—'} />
            <FieldRow label="Subject id" value={row.subject_id ?? '—'} mono />
            <FieldRow label="Tenant" value={row.tenant_id} mono />
            <FieldRow label="Group" value={row.group_id ?? '—'} mono />
            <FieldRow label="Window" value={row.window} />
            <FieldRow
                label="Usage"
                value={`${row.used} / ${row.limit > 0 ? row.limit : '∞'}`}
            />
            <FieldRow
                label="Cooldown"
                value={row.cooldown_active ? 'active' : 'inactive'}
            />
            {row.cooldown_until && (
                <FieldRow label="Cooldown until" value={row.cooldown_until} />
            )}
            <FieldRow
                label="Last updated"
                value={formatTimestamp(row.last_updated_at)}
            />
            <div className="mt-4 text-xs text-tertiary border-t border-default pt-3 space-y-3">
                <p>
                    The gateway holds the authoritative state. The values above are aggregated from the last 60 seconds of usage — they may lag a fast burst by a few seconds. Reset and Release Cooldown reach every replica (typically
                    under 50ms in production).
                </p>
                {/* Outbound deep links — bi-directional incident flow:
                    counter → audit trail (who reset this, when) and
                    counter → proxy logs (what traffic landed against
                    this bucket). The Audit + Proxy Logs pages already
                    support these filter params via their own URL
                    contracts; we just construct the URL and let
                    react-router (or window.location) take it. */}
                {row.subject_id && (
                    <div className="flex flex-wrap gap-2">
                        <a
                            href={
                                `/admin/system/audit?` +
                                `scope_kind=${encodeURIComponent(
                                    deriveScopeKindFromCounterKind(row.kind),
                                )}` +
                                `&scope_id=${encodeURIComponent(row.subject_id)}`
                            }
                            data-testid="system-counters-drawer-audit-link"
                            className="inline-flex items-center gap-1 text-primary hover:underline"
                        >
                            View Audit history →
                        </a>
                        <a
                            href={
                                `/admin/system/proxy-logs?` +
                                `resource_id=${encodeURIComponent(row.subject_id)}` +
                                `&time=last_1h`
                            }
                            data-testid="system-counters-drawer-proxy-logs-link"
                            className="inline-flex items-center gap-1 text-primary hover:underline"
                        >
                            View Proxy Logs →
                        </a>
                    </div>
                )}
            </div>
        </div>
    </div>
);

/**
 * Map a counter-row `kind` to the matching `scope_kind` token used by
 * the Audit page's filter URL. Mirrors the Python
 * `system_operation_log.SCOPE_*` constants (which are the values the
 * Audit endpoint accepts).
 *
 * Falls back to `'counter'` for unknown kinds — the Audit page treats
 * that as "no scope filter", which renders all audit rows for the
 * tenant. Better than a 400 from a malformed scope_kind.
 */
function deriveScopeKindFromCounterKind(kind: CounterKind): string {
    if (kind.startsWith('llm_')) return 'model';
    if (kind.startsWith('mcp_')) return 'mcp_server';
    return 'counter';
}

const FieldRow: React.FC<{ label: string; value: string; mono?: boolean }> = ({
    label,
    value,
    mono,
}) => (
    <div className="flex justify-between gap-3">
        <span className="text-secondary text-xs">{label}</span>
        <span
            className={`text-default text-xs text-right break-all ${mono ? 'font-mono' : ''}`}
        >
            {value}
        </span>
    </div>
);

// ─── Confirm modal ───────────────────────────────────────────────

interface ConfirmIntent {
    kind: 'reset' | 'release';
    row: CounterEntry;
}

interface ConfirmModalProps {
    intent: ConfirmIntent;
    inFlight: boolean;
    error: string | null;
    onCancel: () => void;
    onConfirm: () => void;
}

const ConfirmModal: React.FC<ConfirmModalProps> = ({
    intent,
    inFlight,
    error,
    onCancel,
    onConfirm,
}) => {
    const title =
        intent.kind === 'reset' ? 'Reset counter?' : 'Release cooldown?';
    const summary =
        intent.kind === 'reset'
            ? `This drops the ${intent.row.kind} bucket for ${
                  intent.row.subject_label ??
                  intent.row.subject_id ??
                  intent.row.group_id ??
                  'tenant-wide'
              }. The next request in this window starts from zero.`
            : `This clears any active cooldown for ${
                  intent.row.subject_label ??
                  intent.row.subject_id ??
                  intent.row.group_id ??
                  'tenant-wide'
              }. The proxy will stop rejecting requests against this scope.`;

    return (
        <div
            data-testid="system-counters-confirm"
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
        >
            <div className="bg-surface rounded-lg border border-default shadow-xl max-w-md w-full p-5 space-y-3">
                <h4 className="text-base font-semibold">{title}</h4>
                <p className="text-sm text-secondary">{summary}</p>
                <p className="text-xs text-tertiary">
                    This action is audited. It takes effect on every gateway replica.
                </p>
                {error && (
                    <div
                        data-testid="system-counters-confirm-error"
                        className="text-xs text-red-700 bg-red-50 border border-red-200 rounded p-2"
                    >
                        {error}
                    </div>
                )}
                <div className="flex justify-end gap-2 pt-2">
                    <button
                        type="button"
                        onClick={onCancel}
                        data-testid="system-counters-confirm-cancel"
                        disabled={inFlight}
                        className="px-3 py-1.5 text-sm border border-default rounded hover:bg-surface-muted disabled:opacity-50"
                    >
                        Cancel
                    </button>
                    <button
                        type="button"
                        onClick={onConfirm}
                        data-testid="system-counters-confirm-fire"
                        disabled={inFlight}
                        className="px-3 py-1.5 text-sm bg-primary-600 text-white rounded hover:bg-primary-700 disabled:opacity-50"
                    >
                        {inFlight ? 'Working…' : 'Confirm'}
                    </button>
                </div>
            </div>
        </div>
    );
};

// ─── Helpers ─────────────────────────────────────────────────────

function formatTimestamp(iso: string | null | undefined): string {
    if (!iso) return 'never';
    try {
        return new Date(iso).toLocaleString();
    } catch {
        return iso;
    }
}

export default SystemCountersPane;

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
    Search,
    RefreshCw,
    Download,
    X,
    Copy,
    AlertTriangle,
} from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { TABLE_TH_TYPE, TABLE_THEAD } from '../ui/tableStyles';
import { Pagination } from '../ui/Pagination';
import FacetSidebar from '../monitoring/FacetSidebar';
import TimeRangePicker, { TimeRange } from '../monitoring/TimeRangePicker';
import { timeRangeToBody } from '../monitoring/timeRangeToBody';
import {
    ActorType,
    SystemAuditEntry,
    SystemAuditFilter,
    SystemAuditFacetsResponse,
    SystemAuditListResponse,
} from '../../lib/systemOpsTypes';
import { errorMessage } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';

/**
 * SystemAuditPane.
 *
 * Every destructive action from this console writes one row to
 * `system_operation_log`. This pane is the compliance-trail viewer:
 * faceted search across action / scope / actor, a drawer that diffs
 * the before/after JSONB snapshot, and a CSV export for auditors who
 * live in spreadsheets.
 *
 * Shared components reused from the proxy-logs viewer:
 *
 *   - <FacetSidebar />     left rail with action / scope / actor
 *                          sections, scoped to the active filter.
 *   - <TimeRangePicker />  preset ladder + custom range. Drives the
 *                          start/end the backend filters on.
 *
 * Pagination: server-side. The list endpoint returns `total` along
 * with the page so the footer can render an accurate page count
 * without N round trips. Page size is fixed at PAGE_SIZE to keep the
 * envelope predictable; operators export to CSV when they need more.
 */

const PAGE_SIZE = 50;
const FACET_STORAGE_KEY = 'brutor.systemaudit.facetsidebar';

const SystemAuditPane: React.FC = () => {
    const { gatewayClient } = useGateway();

    // ── Filter state ────────────────────────────────────────────────
    // Mirrors the proxy-logs viewer's filter-envelope shape so the
    // sidebar / time picker / search bar can be wired the same way.
    const [actionFilter, setActionFilter] = useState<string[]>([]);
    const [scopeKindFilter, setScopeKindFilter] = useState<string[]>([]);
    const [actorAdminFilter, setActorAdminFilter] = useState<string[]>([]);
    const [actorTypeFilter, setActorTypeFilter] = useState<string[]>([]);
    const [timeRange, setTimeRange] = useState<TimeRange>({
        kind: 'preset',
        minutes: 60 * 24,
    });
    const [freeTextSearch, setFreeTextSearch] = useState('');
    const [offset, setOffset] = useState(0);

    // ── Data state ──────────────────────────────────────────────────
    const [rows, setRows] = useState<SystemAuditEntry[]>([]);
    const [total, setTotal] = useState(0);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [drawerRow, setDrawerRow] = useState<SystemAuditEntry | null>(null);

    // Facet labels keyed by field — populated from the facets
    // response so the sidebar section headers stay translatable
    // without hard-coding them client-side.
    const [facetLabels, setFacetLabels] = useState<Record<string, string>>({
        action: 'Action',
        scope_kind: 'Scope',
        actor_admin_id: 'Actor',
        actor_type: 'Actor type',
    });

    // ── Filter envelope builder ─────────────────────────────────────
    // Shared between the table fetch and the facet/CSV loaders so
    // every read uses the exact same WHERE clause. Drift between
    // them is the source of "the sidebar said 5 but the table
    // shows 3" bugs.
    const buildFilter = useCallback(
        (): SystemAuditFilter => {
            const body = timeRangeToBody(timeRange);
            return {
                actions: actionFilter.length > 0 ? actionFilter : undefined,
                scope_kinds: scopeKindFilter.length > 0 ? scopeKindFilter : undefined,
                actor_admin_ids:
                    actorAdminFilter.length > 0 ? actorAdminFilter : undefined,
                actor_types:
                    actorTypeFilter.length > 0
                        ? (actorTypeFilter as ActorType[])
                        : undefined,
                // TimeRangePicker speaks the proxy-logs envelope
                // ({hours_ago, start_time, end_time}). Translate to the
                // audit endpoint's explicit start/end. hours_ago is
                // resolved client-side so the wire shape is consistent.
                start: body.start_time
                    ? body.start_time
                    : body.hours_ago !== undefined
                      ? new Date(
                            Date.now() - body.hours_ago * 60 * 60 * 1000,
                        ).toISOString()
                      : undefined,
                end: body.end_time ?? undefined,
                q: freeTextSearch || undefined,
            };
        },
        [
            actionFilter,
            scopeKindFilter,
            actorAdminFilter,
            actorTypeFilter,
            timeRange,
            freeTextSearch,
        ],
    );

    // Refetch key — every filter input contributes a stable hash so
    // the FacetSidebar's open sections re-fetch counts whenever the
    // operator narrows the scope.
    const refetchKey = useMemo(
        () =>
            JSON.stringify({
                actions: actionFilter,
                scope_kinds: scopeKindFilter,
                actor_admin_ids: actorAdminFilter,
                actor_types: actorTypeFilter,
                timeRange,
                q: freeTextSearch,
            }),
        [
            actionFilter,
            scopeKindFilter,
            actorAdminFilter,
            actorTypeFilter,
            timeRange,
            freeTextSearch,
        ],
    );

    // ── Fetch list ──────────────────────────────────────────────────
    const fetchList = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const filter = buildFilter();
            const resp: SystemAuditListResponse =
                await gatewayClient.listSystemAudit({
                    ...filter,
                    limit: PAGE_SIZE,
                    offset,
                });
            setRows(resp.items);
            setTotal(resp.total);
        } catch (e) {
            setError(errorMessage(e) ?? String(e));
        } finally {
            setLoading(false);
        }
    }, [buildFilter, gatewayClient, offset]);

    // Re-fetch when filters or paging change. We use `refetchKey` +
    // `offset` directly rather than `fetchList` to avoid an
    // infinite-loop trigger from `gatewayClient` identity churn (the
    // useGateway hook returns a fresh object each render).
     
    useEffect(() => {
        runBackground(fetchList(), 'list');
    }, [refetchKey, offset]);

    // Reset to page 0 whenever a filter changes — paging across a
    // changed envelope would land on rows that no longer exist in
    // the new result set.
    useEffect(() => {
        setOffset(0);
         
    }, [refetchKey]);

    // ── Facet loader ────────────────────────────────────────────────
    // Calls the audit facets endpoint, scoped to the current filter
    // envelope. The FacetSidebar caches per field, so this fires once
    // per (open field, refetchKey) — not on every keystroke.
    const loadFacets = useCallback(
        async (field: string) => {
            const filter = buildFilter();
            const resp: SystemAuditFacetsResponse =
                await gatewayClient.getSystemAuditFacets(filter);
            // Stash header labels — the backend is the source of
            // truth for the FacetSidebar section copy.
            if (resp.labels && Object.keys(resp.labels).length > 0) {
                setFacetLabels(prev => ({ ...prev, ...resp.labels }));
            }
            const list = resp.facets?.[field] ?? [];
            return list.map(v => ({ value: v.value, count: v.count }));
        },
        [buildFilter, gatewayClient],
    );

    // ── Sidebar wiring ──────────────────────────────────────────────
    const sidebarFields = useMemo(
        () => [
            { field: 'action', label: facetLabels.action ?? 'Action' },
            { field: 'scope_kind', label: facetLabels.scope_kind ?? 'Scope' },
            {
                field: 'actor_admin_id',
                label: facetLabels.actor_admin_id ?? 'Actor',
            },
            {
                field: 'actor_type',
                label: facetLabels.actor_type ?? 'Actor type',
            },
        ],
        [facetLabels],
    );

    const sidebarSelected = useMemo(
        () => ({
            action: actionFilter,
            scope_kind: scopeKindFilter,
            actor_admin_id: actorAdminFilter,
            actor_type: actorTypeFilter,
        }),
        [actionFilter, scopeKindFilter, actorAdminFilter, actorTypeFilter],
    );

    const handleSidebarChange = useCallback(
        (field: string, next: string[]) => {
            switch (field) {
                case 'action':
                    setActionFilter(next);
                    break;
                case 'scope_kind':
                    setScopeKindFilter(next);
                    break;
                case 'actor_admin_id':
                    setActorAdminFilter(next);
                    break;
                case 'actor_type':
                    setActorTypeFilter(next);
                    break;
            }
        },
        [],
    );

    // ── Export CSV ─────────────────────────────────────────────────
    const handleExport = useCallback(async () => {
        try {
            const filter = buildFilter();
            const { blob, filename } =
                await gatewayClient.downloadSystemAuditCsv(filter);
            // Trigger the browser download. Same pattern as the
            // proxy-logs export — Blob + temporary anchor.
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = filename ?? 'system-audit.csv';
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(url);
        } catch (e) {
            setError(errorMessage(e) ?? String(e));
        }
    }, [buildFilter, gatewayClient]);

    // ── Render ──────────────────────────────────────────────────────
    return (
        <div
            data-testid="system-audit-pane"
            className="app-column h-full min-h-0"
        >
            {/* ── Toolbar ─────────────────────────────────────── */}
            <header className="flex flex-wrap items-center gap-3 pb-3 border-b border-default">
                <TimeRangePicker value={timeRange} onChange={setTimeRange} />
                <button
                    type="button"
                    onClick={asyncHandler(fetchList, 'click')}
                    data-testid="system-audit-refresh"
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-default hover:bg-surface-muted"
                >
                    <RefreshCw
                        className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`}
                    />
                    Refresh
                </button>
                <button
                    type="button"
                    onClick={asyncHandler(handleExport, 'click')}
                    data-testid="system-audit-export-csv"
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-default hover:bg-surface-muted"
                    title="Export filtered rows as CSV"
                >
                    <Download className="w-4 h-4" />
                    Export CSV
                </button>
                <div className="ml-auto text-xs text-tertiary">
                    {total.toLocaleString()} matching row
                    {total === 1 ? '' : 's'}
                </div>
            </header>

            {/* ── Two-column layout: sidebar + content ────────── */}
            <div className="flex flex-1 min-h-0 min-w-0">
                <FacetSidebar
                    fields={sidebarFields}
                    selected={sidebarSelected}
                    onChange={handleSidebarChange}
                    loadFacets={loadFacets}
                    storageKey={FACET_STORAGE_KEY}
                    refetchKey={refetchKey}
                />

                <div className="flex flex-col flex-1 min-w-0 min-h-0">
                    {/* ── Free-text search bar ──────────────── */}
                    <div className="border-b border-default bg-surface px-3 py-2 flex items-center gap-2">
                        <Search className="w-4 h-4 text-muted flex-shrink-0" />
                        <input
                            type="text"
                            value={freeTextSearch}
                            data-testid="system-audit-search"
                            onChange={e => setFreeTextSearch(e.target.value)}
                            placeholder="Search payload, before/after state, scope id…"
                            className="flex-1 px-2 py-1 text-xs bg-transparent border-none focus:outline-none min-h-0 min-w-0"
                        />
                        {freeTextSearch && (
                            <button
                                type="button"
                                onClick={() => setFreeTextSearch('')}
                                className="text-[11px] text-muted hover:text-default"
                            >
                                Clear
                            </button>
                        )}
                    </div>

                    {/* ── Error banner ──────────────────────── */}
                    {error && (
                        <div className="mx-4 mt-3 p-3 bg-red-50 border border-red-200 rounded-lg">
                            <div className="flex items-start gap-2">
                                <AlertTriangle className="w-4 h-4 text-red-600 mt-0.5 flex-shrink-0" />
                                <div className="text-sm text-red-800">
                                    {error}
                                </div>
                            </div>
                        </div>
                    )}

                    {/* ── Table ─────────────────────────────── */}
                    <div className="scroll-region">
                        <AuditTable
                            rows={rows}
                            loading={loading}
                            onSelectRow={setDrawerRow}
                        />
                    </div>

                    {/* ── Pagination ────────────────────────── */}
                    <Pagination
                        offset={offset}
                        pageSize={PAGE_SIZE}
                        total={total}
                        onChange={setOffset}
                        testIdPrefix="system-audit"
                        className="px-3 py-2 border-t border-default"
                    />
                </div>
            </div>

            {/* ── Drawer ───────────────────────────────────────── */}
            {drawerRow && (
                <AuditRowDrawer
                    row={drawerRow}
                    onClose={() => setDrawerRow(null)}
                />
            )}
        </div>
    );
};

// ─── Table ───────────────────────────────────────────────────────

interface AuditTableProps {
    rows: SystemAuditEntry[];
    loading: boolean;
    onSelectRow: (row: SystemAuditEntry) => void;
}

const AuditTable: React.FC<AuditTableProps> = ({
    rows,
    loading,
    onSelectRow,
}) => {
    if (loading && rows.length === 0) {
        return (
            <div className="p-6 text-sm text-secondary italic">
                Loading audit rows…
            </div>
        );
    }
    if (rows.length === 0) {
        return (
            <div
                data-testid="system-audit-empty"
                className="p-6 text-sm text-tertiary italic"
            >
                No audit rows match the current filters.
            </div>
        );
    }
    return (
        <table className="w-full text-xs">
            <thead className={`${TABLE_THEAD} sticky top-0`}>
                <tr>
                    <th className={`text-left px-3 py-2 ${TABLE_TH_TYPE}`}>When</th>
                    <th className={`text-left px-3 py-2 ${TABLE_TH_TYPE}`}>Actor</th>
                    <th className={`text-left px-3 py-2 ${TABLE_TH_TYPE}`}>Action</th>
                    <th className={`text-left px-3 py-2 ${TABLE_TH_TYPE}`}>Scope</th>
                    <th className={`text-left px-3 py-2 ${TABLE_TH_TYPE}`}>IP</th>
                </tr>
            </thead>
            <tbody>
                {rows.map(r => (
                    <tr
                        key={r.id}
                        data-testid={`system-audit-row-${r.id}`}
                        onClick={() => onSelectRow(r)}
                        className="border-b border-default hover:bg-surface-muted cursor-pointer"
                    >
                        <td className="px-3 py-1.5 whitespace-nowrap text-default">
                            {formatTs(r.created_at)}
                        </td>
                        <td className="px-3 py-1.5 text-default">
                            <div className="font-mono">{r.actor_admin_id}</div>
                            <div className="text-[10px] text-tertiary">
                                {r.actor_type}
                            </div>
                        </td>
                        <td className="px-3 py-1.5">
                            <code className="px-1.5 py-0.5 rounded bg-surface-alt">
                                {r.action}
                            </code>
                        </td>
                        <td className="px-3 py-1.5 text-default">
                            {r.scope_kind ? (
                                <>
                                    <span className="text-secondary">
                                        {r.scope_kind}:
                                    </span>{' '}
                                    <span className="font-mono">
                                        {r.scope_id ?? '—'}
                                    </span>
                                </>
                            ) : (
                                <span className="text-tertiary">—</span>
                            )}
                        </td>
                        <td className="px-3 py-1.5 text-tertiary font-mono">
                            {r.ip ?? ''}
                        </td>
                    </tr>
                ))}
            </tbody>
        </table>
    );
};

// ─── Drawer ─────────────────────────────────────────────────────

interface DrawerProps {
    row: SystemAuditEntry;
    onClose: () => void;
}

/**
 * Full-row drawer. Renders three JSON panes:
 *
 *   - payload     — the sanitised request body (secrets redacted by
 *                   `write_system_operation` at write time)
 *   - before / after — the runtime state diff. Side-by-side so the
 *                   auditor can spot what changed without a heavyweight
 *                   JSON-diff library; the pre-render formatting keeps
 *                   the keys aligned.
 *
 * Copy-to-clipboard buttons sit next to each pane so the auditor can
 * paste the snapshot into an incident channel without a "select all
 * → escape weird chars" dance.
 */
const AuditRowDrawer: React.FC<DrawerProps> = ({ row, onClose }) => {
    return (
        <div className="fixed inset-0 z-50 flex">
            <div
                className="flex-1 bg-black/40 min-h-0 min-w-0"
                onClick={onClose}
                data-testid="system-audit-drawer-backdrop"
            />
            <div
                data-testid="system-audit-drawer"
                className="w-[640px] max-w-full bg-surface border-l border-default overflow-y-auto"
            >
                <header className="flex items-start justify-between px-4 py-3 border-b border-default sticky top-0 bg-surface z-10">
                    <div>
                        <div className="text-sm font-semibold text-default">
                            {row.action}
                        </div>
                        <div className="text-[11px] text-tertiary font-mono mt-0.5">
                            {row.id}
                        </div>
                    </div>
                    <button
                        type="button"
                        onClick={onClose}
                        aria-label="Close drawer"
                        className="text-secondary hover:text-default"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </header>
                <dl className="grid grid-cols-2 gap-x-4 gap-y-2 px-4 py-3 text-xs border-b border-default">
                    <dt className="text-tertiary">When</dt>
                    <dd className="text-default">{formatTs(row.created_at)}</dd>
                    <dt className="text-tertiary">Actor</dt>
                    <dd className="text-default font-mono">
                        {row.actor_admin_id} ({row.actor_type})
                    </dd>
                    <dt className="text-tertiary">Tenant</dt>
                    <dd className="text-default font-mono">
                        {row.tenant_id ?? '—'}
                    </dd>
                    <dt className="text-tertiary">Scope</dt>
                    <dd className="text-default">
                        {row.scope_kind ? (
                            <>
                                <span className="text-secondary">
                                    {row.scope_kind}:
                                </span>{' '}
                                <span className="font-mono">
                                    {row.scope_id ?? '—'}
                                </span>
                            </>
                        ) : (
                            '—'
                        )}
                    </dd>
                    <dt className="text-tertiary">IP / User-Agent</dt>
                    <dd className="text-default font-mono break-all">
                        {row.ip ?? '—'}
                        <div className="text-tertiary text-[10px] mt-0.5">
                            {row.user_agent ?? ''}
                        </div>
                    </dd>
                </dl>
                <JsonPane title="Payload" data={row.payload} />
                {/* Side-by-side before/after grid so a diff is
                    immediately scannable. The two-column layout
                    deliberately avoids a heavyweight diff library —
                    operators are good at eyeballing two columns of
                    JSON, and the bundle cost of a JSON-diff lib is
                    only worth paying when payloads routinely exceed
                    ~50 lines. */}
                <div className="grid grid-cols-2 divide-x divide-default">
                    <JsonPane title="Before" data={row.before_state} />
                    <JsonPane title="After" data={row.after_state} />
                </div>
            </div>
        </div>
    );
};

interface JsonPaneProps {
    title: string;
    data: any;
}

const JsonPane: React.FC<JsonPaneProps> = ({ title, data }) => {
    const json = useMemo(() => {
        if (data == null) return null;
        try {
            return JSON.stringify(data, null, 2);
        } catch {
            // Defensive — payloads are JSONB so this should always
            // serialise, but a stray non-serialisable value in a
            // future audit row shouldn't crash the drawer.
            return String(data);
        }
    }, [data]);

    const onCopy = useCallback(() => {
        if (!json) return;
        // navigator.clipboard isn't available in jsdom test env;
        // guard so the click handler stays defensive.
        const clip = (navigator as any).clipboard;
        if (clip && typeof clip.writeText === 'function') {
            clip.writeText(json);
        }
    }, [json]);

    return (
        <section
            data-testid={`system-audit-drawer-${title.toLowerCase()}`}
            className="px-4 py-3"
        >
            <header className="flex items-center justify-between mb-1.5">
                <span className="text-[11px] font-semibold uppercase tracking-wide text-secondary">
                    {title}
                </span>
                {json && (
                    <button
                        type="button"
                        onClick={onCopy}
                        className="inline-flex items-center gap-1 text-[11px] text-secondary hover:text-default"
                        title={`Copy ${title} to clipboard`}
                    >
                        <Copy className="w-3 h-3" />
                        Copy
                    </button>
                )}
            </header>
            {json ? (
                <pre className="text-[11px] font-mono whitespace-pre-wrap break-words bg-surface-alt p-2 rounded">
                    {json}
                </pre>
            ) : (
                <div className="text-[11px] text-tertiary italic">empty</div>
            )}
        </section>
    );
};

// ─── helpers ─────────────────────────────────────────────────────

function formatTs(iso: string | null): string {
    if (!iso) return '—';
    try {
        return new Date(iso).toLocaleString();
    } catch {
        return iso;
    }
}

export default SystemAuditPane;

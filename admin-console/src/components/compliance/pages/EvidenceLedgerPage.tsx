/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Compliance → Evidence Ledger: the record explorer.
 *
 * Filters (system, all 14 verdicts, kind, surface, run, window), a
 * paginated table of sealed records with a chain shortcut per row, a
 * verdict distribution strip for the same window, the chain view around
 * one record, the tenant log with its witnesses, backlog, heads and
 * receipts, the Verify panel, bundle export, and the disclosure records —
 * and, on a row, the shared record dialog with the proof and Verify.
 */

import { useCallback, useState } from 'react';
import { Link2, Loader2, RefreshCw } from 'lucide-react';

import { useGateway } from '../../../hooks/useGateway';
import { useKeyedLoad } from '../../../hooks/useKeyedLoad';
import {
    chipFor,
    RECORD_KINDS,
    RECORD_VERDICTS,
    shortHash,
    VERDICT_CHIP,
    type EvidenceRecordFilters,
    type EvidenceRecordListResponse,
    type EvidenceSummary,
} from '../../../lib/complianceEvidenceTypes';
import { readChain } from '../../../lib/evidenceSurfaceTypes';
import { TABLE_CONTAINER, TABLE_ROW_HOVER, TABLE_TBODY, TABLE_TH, TABLE_THEAD } from '../../ui/tableStyles';
import BundleExportPanel from '../BundleExportPanel';
import ChainPanel from '../ChainPanel';
import LogPanel from '../LogPanel';
import RecordDialog from '../RecordDialog';
import VerifyPanel from '../VerifyPanel';
import { formatWhen } from '../complianceFormat';
import type { CompliancePageProps } from '../complianceSections';

const PAGE_LIMIT = 50;

type FilterState = Required<Pick<EvidenceRecordFilters, 'ai_system_id' | 'verdict' | 'kind' | 'surface' | 'root_task_id' | 'since' | 'until'>>;

const EMPTY_FILTERS: FilterState = {
    ai_system_id: '',
    verdict: '',
    kind: '',
    surface: '',
    root_task_id: '',
    since: '',
    until: '',
};

const INPUT = 'rounded-md border border-default bg-surface px-2 py-1.5 text-xs';

interface LedgerData {
    list: EvidenceRecordListResponse;
    summary: EvidenceSummary;
}

export default function EvidenceLedgerPage({ framework, frameworks }: CompliancePageProps) {
    const { gatewayClient, fetchAISystems } = useGateway();
    const [filters, setFilters] = useState<FilterState>(EMPTY_FILTERS);
    const [applied, setApplied] = useState<FilterState>(EMPTY_FILTERS);
    const [offset, setOffset] = useState(0);
    const [openRecord, setOpenRecord] = useState<string | null>(null);
    const [chainRecord, setChainRecord] = useState<string | null>(null);

    // The system list only names rows; a failure leaves ids on screen.
    const systemsLoader = useCallback(() => fetchAISystems({ limit: 200 }), [fetchAISystems]);
    const systems = useKeyedLoad('systems', systemsLoader).data?.systems ?? [];
    const disclosuresLoader = useCallback(() => gatewayClient.listEvidenceDisclosures({ limit: 20 }), [gatewayClient]);
    const disclosuresLoad = useKeyedLoad('disclosures', disclosuresLoader);
    const disclosures = disclosuresLoad.data?.items ?? (disclosuresLoad.loading ? null : []);
    const reloadDisclosures = disclosuresLoad.reload;

    const loader = useCallback(async (): Promise<LedgerData> => {
        const since = applied.since ? `${applied.since}T00:00:00Z` : undefined;
        const until = applied.until ? `${applied.until}T23:59:59Z` : undefined;
        const [list, summary] = await Promise.all([
            gatewayClient.listEvidenceRecords({
                ai_system_id: applied.ai_system_id || undefined,
                verdict: applied.verdict || undefined,
                kind: applied.kind || undefined,
                surface: applied.surface || undefined,
                root_task_id: applied.root_task_id || undefined,
                since,
                until,
                limit: PAGE_LIMIT,
                offset,
            }),
            gatewayClient.getEvidenceSummary({ since, until, ai_system_id: applied.ai_system_id || undefined }),
        ]);
        return { list, summary };
    }, [gatewayClient, applied, offset]);
    const { data, error, loading, reload } = useKeyedLoad(
        JSON.stringify({ applied, offset }),
        loader,
        'Failed to load records',
    );
    const items = data?.list.items ?? [];
    const total = data?.list.total ?? 0;
    const summary = data?.summary ?? null;

    const page = Math.floor(offset / PAGE_LIMIT) + 1;
    const pageCount = Math.max(1, Math.ceil(total / PAGE_LIMIT));
    const systemName = (id: string | null | undefined) =>
        id ? (systems.find((s) => s.id === id)?.display_name ?? systems.find((s) => s.id === id)?.name ?? id) : '—';

    return (
        <div className="space-y-4" data-testid="compliance-evidence">
            {/* Filters */}
            <form
                className="rounded-lg border border-default bg-surface p-3 flex flex-wrap items-end gap-2"
                onSubmit={(e) => {
                    e.preventDefault();
                    setOffset(0);
                    setApplied(filters);
                }}
                data-testid="ledger-filters"
            >
                <label className="block">
                    <span className="text-[10px] text-muted uppercase">System</span>
                    <select
                        value={filters.ai_system_id}
                        onChange={(e) => setFilters({ ...filters, ai_system_id: e.target.value })}
                        className={`${INPUT} block`}
                        data-testid="ledger-filter-system"
                    >
                        <option value="">any</option>
                        {systems.map((s) => (
                            <option key={s.id} value={s.id}>
                                {s.display_name || s.name}
                            </option>
                        ))}
                    </select>
                </label>
                <label className="block">
                    <span className="text-[10px] text-muted uppercase">Verdict</span>
                    <select
                        value={filters.verdict}
                        onChange={(e) => setFilters({ ...filters, verdict: e.target.value })}
                        className={`${INPUT} block`}
                        data-testid="ledger-filter-verdict"
                    >
                        <option value="">any</option>
                        {RECORD_VERDICTS.map((v) => (
                            <option key={v} value={v}>
                                {v}
                            </option>
                        ))}
                    </select>
                </label>
                <label className="block">
                    <span className="text-[10px] text-muted uppercase">Kind</span>
                    <select
                        value={filters.kind}
                        onChange={(e) => setFilters({ ...filters, kind: e.target.value })}
                        className={`${INPUT} block`}
                        data-testid="ledger-filter-kind"
                    >
                        <option value="">any</option>
                        {RECORD_KINDS.map((k) => (
                            <option key={k} value={k}>
                                {k}
                            </option>
                        ))}
                    </select>
                </label>
                <label className="block">
                    <span className="text-[10px] text-muted uppercase">Surface</span>
                    <input
                        type="text"
                        value={filters.surface}
                        onChange={(e) => setFilters({ ...filters, surface: e.target.value })}
                        placeholder="llm, mcp, a2a…"
                        className={`${INPUT} block w-28`}
                        data-testid="ledger-filter-surface"
                    />
                </label>
                <label className="block">
                    <span className="text-[10px] text-muted uppercase">Run id</span>
                    <input
                        type="text"
                        value={filters.root_task_id}
                        onChange={(e) => setFilters({ ...filters, root_task_id: e.target.value })}
                        className={`${INPUT} block w-44 font-mono`}
                        data-testid="ledger-filter-run"
                    />
                </label>
                <label className="block">
                    <span className="text-[10px] text-muted uppercase">Since</span>
                    <input
                        type="date"
                        value={filters.since}
                        onChange={(e) => setFilters({ ...filters, since: e.target.value })}
                        className={`${INPUT} block`}
                        data-testid="ledger-filter-since"
                    />
                </label>
                <label className="block">
                    <span className="text-[10px] text-muted uppercase">Until</span>
                    <input
                        type="date"
                        value={filters.until}
                        onChange={(e) => setFilters({ ...filters, until: e.target.value })}
                        className={`${INPUT} block`}
                        data-testid="ledger-filter-until"
                    />
                </label>
                <button
                    type="submit"
                    className="px-3 py-1.5 text-xs font-medium rounded-md bg-blue-600 text-white hover:bg-blue-700"
                    data-testid="ledger-apply"
                >
                    Apply
                </button>
                <button
                    type="button"
                    onClick={() => {
                        setFilters(EMPTY_FILTERS);
                        setApplied(EMPTY_FILTERS);
                        setOffset(0);
                    }}
                    className="px-3 py-1.5 text-xs font-medium rounded-md border border-strong hover:bg-surface-hover"
                >
                    Clear
                </button>
                <button
                    type="button"
                    onClick={reload}
                    disabled={loading}
                    className="ml-auto inline-flex items-center gap-1 px-2.5 py-1.5 text-xs border border-strong rounded-md hover:bg-surface-hover disabled:opacity-50"
                >
                    <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} /> Refresh
                </button>
            </form>

            {/* Summary strip */}
            {summary && (
                <div className="flex flex-wrap items-center gap-2 text-xs" data-testid="ledger-summary">
                    <span className="text-secondary">
                        {summary.records_total.toLocaleString()} sealed · {summary.human_disposed_count.toLocaleString()} human-disposed ·{' '}
                        {summary.systems_covered} system{summary.systems_covered === 1 ? '' : 's'}
                    </span>
                    {Object.entries(summary.by_verdict)
                        .sort((a, b) => b[1] - a[1])
                        .map(([verdict, count]) => (
                            <button
                                key={verdict}
                                type="button"
                                onClick={() => {
                                    const next = { ...filters, verdict };
                                    setFilters(next);
                                    setApplied(next);
                                    setOffset(0);
                                }}
                                className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${chipFor(VERDICT_CHIP, verdict)}`}
                                data-testid={`ledger-verdict-${verdict}`}
                            >
                                {verdict} {count.toLocaleString()}
                            </button>
                        ))}
                </div>
            )}

            {error && (
                <p className="p-3 rounded-lg border border-red-300 bg-red-50 text-sm text-red-800" data-testid="ledger-error">
                    {error}
                </p>
            )}

            {/* Records */}
            <div className={TABLE_CONTAINER}>
                <table className="w-full text-xs">
                    <thead className={TABLE_THEAD}>
                        <tr>
                            <th className={TABLE_TH}>Timestamp</th>
                            <th className={TABLE_TH}>Verdict</th>
                            <th className={TABLE_TH}>Kind</th>
                            <th className={TABLE_TH}>Surface</th>
                            <th className={TABLE_TH}>Effect</th>
                            <th className={TABLE_TH}>System</th>
                            <th className={TABLE_TH}>Human</th>
                            <th className={TABLE_TH}>Chain</th>
                            <th className={`${TABLE_TH} text-right`}>Seq</th>
                        </tr>
                    </thead>
                    <tbody className={TABLE_TBODY}>
                        {loading && items.length === 0 && (
                            <tr>
                                <td colSpan={9} className="px-4 py-6 text-center text-muted">
                                    <Loader2 className="w-4 h-4 animate-spin inline-block mr-1" /> Loading…
                                </td>
                            </tr>
                        )}
                        {!loading && items.length === 0 && !error && (
                            <tr>
                                <td colSpan={9} className="px-4 py-6 text-center text-muted" data-testid="ledger-empty">
                                    No sealed records match these filters.
                                </td>
                            </tr>
                        )}
                        {items.map((r) => (
                            <tr
                                key={r.record_id}
                                className={`${TABLE_ROW_HOVER} cursor-pointer`}
                                onClick={() => setOpenRecord(r.record_id)}
                                data-testid={`record-row-${r.record_id}`}
                            >
                                <td className="px-4 py-2 text-secondary whitespace-nowrap">
                                    {r.timestamp ? new Date(r.timestamp).toLocaleString() : '—'}
                                </td>
                                <td className="px-4 py-2">
                                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${chipFor(VERDICT_CHIP, r.verdict)}`}>
                                        {r.verdict}
                                    </span>
                                </td>
                                <td className="px-4 py-2 text-secondary">{r.kind}</td>
                                <td className="px-4 py-2 text-secondary">{r.surface}</td>
                                <td className="px-4 py-2 text-secondary">{r.effect_type ?? '—'}</td>
                                <td className="px-4 py-2 text-default truncate max-w-[12rem]" title={r.ai_system_id ?? undefined}>
                                    {systemName(r.ai_system_id)}
                                </td>
                                <td className="px-4 py-2 text-secondary">{r.human_disposed ? 'yes' : '—'}</td>
                                <td className="px-4 py-2" onClick={(e) => e.stopPropagation()}>
                                    <button
                                        type="button"
                                        className="inline-flex items-center gap-1 text-primary-600 hover:underline"
                                        onClick={() => setChainRecord(r.record_id)}
                                        title={readChain(r.chain) ? `${readChain(r.chain)?.relation} ${shortHash(readChain(r.chain)?.parentRecordId, 12)}` : 'starts a chain'}
                                        data-testid={`record-chain-${r.record_id}`}
                                    >
                                        <Link2 className="w-3 h-3" />
                                        {readChain(r.chain)?.relation ?? 'root'}
                                    </button>
                                </td>
                                <td className="px-4 py-2 text-right font-mono text-secondary">{r.seq}</td>
                            </tr>
                        ))}
                    </tbody>
                </table>
                <div className="flex items-center justify-between border-t border-default px-4 py-2">
                    <span className="text-xs text-muted">
                        {total.toLocaleString()} record{total === 1 ? '' : 's'}
                    </span>
                    {total > PAGE_LIMIT && (
                        <div className="flex items-center gap-2">
                            <button
                                type="button"
                                onClick={() => setOffset(Math.max(0, offset - PAGE_LIMIT))}
                                disabled={offset === 0}
                                className="px-3 py-1 text-xs font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt disabled:opacity-50 disabled:cursor-not-allowed"
                                data-testid="ledger-prev"
                            >
                                Prev
                            </button>
                            <span className="text-xs text-muted">
                                Page {page} of {pageCount}
                            </span>
                            <button
                                type="button"
                                onClick={() => setOffset(offset + PAGE_LIMIT)}
                                disabled={offset + PAGE_LIMIT >= total}
                                className="px-3 py-1 text-xs font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt disabled:opacity-50 disabled:cursor-not-allowed"
                                data-testid="ledger-next"
                            >
                                Next
                            </button>
                        </div>
                    )}
                </div>
            </div>

            {chainRecord && (
                <ChainPanel key={chainRecord} recordId={chainRecord} onOpenRecord={setOpenRecord} onClose={() => setChainRecord(null)} />
            )}

            <LogPanel />

            <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
                <VerifyPanel />
                <BundleExportPanel
                    systems={systems.map((s) => ({ id: s.id, name: s.display_name || s.name }))}
                    frameworks={frameworks}
                    framework={framework}
                    onDisclosed={reloadDisclosures}
                    onOpenRecord={setOpenRecord}
                />
            </div>

            {/* Disclosures */}
            <section className="rounded-lg border border-default bg-surface" data-testid="disclosures-panel">
                <div className="px-4 py-3 border-b border-default">
                    <h3 className="text-sm font-semibold text-default">Disclosures</h3>
                    <p className="text-xs text-muted">
                        Each disclosure records what was handed to whom, which records it covered, the bundle digest and
                        which fields were withheld. Sealed in the tenant log like any other record.
                    </p>
                </div>
                {disclosures === null ? (
                    <p className="px-4 py-4 text-xs text-muted">Loading…</p>
                ) : disclosures.length === 0 ? (
                    <p className="px-4 py-4 text-xs text-muted" data-testid="disclosures-empty">
                        No disclosure records yet — build a bundle with an audience to create one.
                    </p>
                ) : (
                    <ul className="divide-y divide-default">
                        {disclosures.map((d) => (
                            <li key={d.id} className="px-4 py-2 text-xs flex items-center gap-3 flex-wrap" data-testid={`disclosure-${d.id}`}>
                                <span className="text-secondary whitespace-nowrap">{formatWhen(d.created_at)}</span>
                                <span className="text-default">to {d.audience}</span>
                                <span className="text-muted">
                                    {d.disclosed_count} record{d.disclosed_count === 1 ? '' : 's'} · {d.completeness} ·{' '}
                                    {Object.keys(d.selector).join(', ') || 'selector unknown'}
                                </span>
                                {d.bundle_digest && (
                                    <code className="font-mono text-secondary" title={d.bundle_digest}>
                                        digest {shortHash(d.bundle_digest, 16)}
                                    </code>
                                )}
                                {d.suppressed_fields.length > 0 && (
                                    <span className="text-amber-800">{d.suppressed_fields.length} field(s) withheld</span>
                                )}
                                {d.seq !== null && d.seq !== undefined && <span className="text-muted ml-auto">seq {d.seq}</span>}
                            </li>
                        ))}
                    </ul>
                )}
            </section>

            {openRecord && <RecordDialog recordId={openRecord} onClose={() => setOpenRecord(null)} />}
        </div>
    );
}

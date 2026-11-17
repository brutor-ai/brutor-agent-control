/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The tenant log: its size, root and key id, the unwitnessed
 * backlog, the configured witnesses with the verdict of the latest receipt
 * each produced, and the tree heads with their receipts. A head's grade is
 * the minimum the receipts earn — standalone (no receipt), witnessed (one
 * receipt under a pinned key), plural (two independently operated
 * witnesses), self-witnessed (a witness the same operator runs). Grades
 * never go up.
 */

import { useCallback } from 'react';
import { Loader2, RefreshCw } from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { useKeyedLoad } from '../../hooks/useKeyedLoad';
import {
    chipFor,
    HEAD_GRADE_CHIP,
    headGradeLabel,
    RECEIPT_VERDICT_CHIP,
    receiptVerdictLabel,
    shortHash,
    type EvidenceLogItem,
    type EvidenceTreeHead,
    type EvidenceWitness,
} from '../../lib/complianceEvidenceTypes';
import { TABLE_CONTAINER, TABLE_TBODY, TABLE_TH, TABLE_THEAD } from '../ui/tableStyles';
import { latestReceiptByWitness } from './ledgerHelpers';
import { failureSentence } from '../../lib/narrow';
import ReadFailureNotice from '../common/ReadFailureNotice';

const HEADS_SHOWN = 10;

interface LogView {
    log: EvidenceLogItem | null;
    heads: EvidenceTreeHead[];
    headsTotal: number;
    witnesses: EvidenceWitness[] | null;
    unwitnessedHeads: number | null;
    /** Why the witnesses, or the backlog, are null: the sentence of the read that failed, or ''. */
    witnessesFailure: string;
    backlogFailure: string;
}

/** The answer of a side read, or null with the sentence of its failure. */
async function sideRead<T>(what: string, read: Promise<T>): Promise<{ value: T | null; failure: string }> {
    return read.then(
        (value) => ({ value, failure: '' }),
        (error: unknown) => ({ value: null, failure: failureSentence(what, error) }),
    );
}

export default function LogPanel() {
    const { gatewayClient } = useGateway();

    const loader = useCallback(async (): Promise<LogView> => {
        // Witnesses and the backlog are side information: either may fail
        // without taking the log down with it.
        const [res, w, s] = await Promise.all([
            gatewayClient.listEvidenceLogs(),
            sideRead('Failed to load the witnesses', gatewayClient.listEvidenceWitnesses().then((r) => r.items)),
            sideRead('Failed to load the unwitnessed backlog', gatewayClient.getEvidenceSummary().then((r) => r.log?.unwitnessed_heads ?? 0)),
        ]);
        const side = { witnesses: w.value, unwitnessedHeads: s.value, witnessesFailure: w.failure, backlogFailure: s.failure };
        const log = res.items[0] ?? null;
        if (!log) return { log: null, heads: [], headsTotal: 0, ...side };
        const h = await gatewayClient.listEvidenceTreeHeads(log.log_id, { limit: HEADS_SHOWN });
        return { log, heads: h.items, headsTotal: h.total, ...side };
    }, [gatewayClient]);

    const { data, error, loading, reload } = useKeyedLoad('log', loader, 'Failed to load the tenant log');
    const log = data?.log ?? null;
    const heads = data?.heads ?? [];
    const headsTotal = data?.headsTotal ?? 0;
    const witnesses = data?.witnesses ?? null;
    const unwitnessed = data?.unwitnessedHeads ?? null;
    const latestReceipts = latestReceiptByWitness(heads);

    return (
        <section className="rounded-lg border border-default bg-surface" data-testid="log-panel">
            <div className="flex items-center justify-between px-4 py-3 border-b border-default">
                <div>
                    <h3 className="text-sm font-semibold text-default">Tenant log</h3>
                    <p className="text-xs text-muted">
                        Every sealed record is a leaf; a tree head seals the log at a size and a witness
                        countersigns it.
                    </p>
                </div>
                <button
                    type="button"
                    onClick={reload}
                    disabled={loading}
                    className="inline-flex items-center gap-1 px-2.5 py-1 text-xs border border-strong rounded-lg hover:bg-surface-hover disabled:opacity-50"
                >
                    <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} /> Refresh
                </button>
            </div>

            {error && (
                <p className="px-4 py-3 text-xs text-red-700" data-testid="log-panel-error">
                    {error}
                </p>
            )}
            {loading && !data && !error && (
                <p className="px-4 py-6 text-xs text-muted flex items-center gap-2">
                    <Loader2 className="w-3.5 h-3.5 animate-spin" /> Loading…
                </p>
            )}
            {!loading && !error && !log && (
                <p className="px-4 py-6 text-xs text-muted" data-testid="log-panel-empty">
                    No tenant log yet — the core creates it with the first sealed record.
                </p>
            )}

            {log && (
                <>
                    <dl className="grid grid-cols-2 md:grid-cols-5 gap-x-6 gap-y-2 px-4 py-3 text-xs" data-testid="log-panel-status">
                        <div>
                            <dt className="text-muted uppercase tracking-wide">Tree size</dt>
                            <dd className="text-default text-base font-semibold">{log.tree_size.toLocaleString()}</dd>
                        </div>
                        <div>
                            <dt className="text-muted uppercase tracking-wide">Root</dt>
                            <dd className="text-default font-mono" title={log.root ?? undefined}>{shortHash(log.root)}</dd>
                        </div>
                        <div>
                            <dt className="text-muted uppercase tracking-wide">Key id</dt>
                            <dd className="text-default font-mono" title={log.kid ?? undefined}>{shortHash(log.kid, 16)}</dd>
                        </div>
                        <div>
                            <dt className="text-muted uppercase tracking-wide">Latest head</dt>
                            <dd>
                                {log.last_tree_head ? (
                                    <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${chipFor(HEAD_GRADE_CHIP, log.last_tree_head.grade)}`}>
                                        {headGradeLabel(log.last_tree_head.grade)}
                                    </span>
                                ) : (
                                    <span className="text-muted">none yet</span>
                                )}
                                <span className="ml-2 text-muted">
                                    {log.heads_count} head{log.heads_count === 1 ? '' : 's'} · {log.receipts_count} receipt{log.receipts_count === 1 ? '' : 's'}
                                </span>
                            </dd>
                        </div>
                        <div>
                            <dt className="text-muted uppercase tracking-wide">Unwitnessed backlog</dt>
                            <dd data-testid="log-panel-backlog">
                                {unwitnessed === null ? (
                                    data?.backlogFailure ? (
                                        <ReadFailureNotice compact failure={{ sentence: data.backlogFailure, loadedAt: null }} onRetry={reload} />
                                    ) : (
                                        <span className="text-muted">not evaluable</span>
                                    )
                                ) : unwitnessed > 0 ? (
                                    <span className="text-amber-800 font-semibold">
                                        {unwitnessed} head{unwitnessed === 1 ? '' : 's'} without a receipt
                                    </span>
                                ) : (
                                    <span className="text-secondary">none</span>
                                )}
                            </dd>
                        </div>
                    </dl>

                    <div className="px-4 pb-3 text-xs" data-testid="log-panel-witnesses">
                        <h4 className="text-muted uppercase tracking-wide mb-1">Witnesses</h4>
                        {witnesses === null ? (
                            <ReadFailureNotice
                                compact
                                failure={{ sentence: data?.witnessesFailure || 'Failed to load the witnesses', loadedAt: null }}
                                onRetry={reload}
                            />
                        ) : witnesses.length === 0 ? (
                            <p className="text-amber-800" data-testid="log-panel-no-witnesses">
                                No witness configured — every head stays standalone until one is (Settings → Tenant → Evidence).
                            </p>
                        ) : (
                            <ul className="flex flex-wrap gap-2">
                                {witnesses.map((w) => {
                                    const latest = latestReceipts.get(w.id);
                                    return (
                                        <li key={w.id} className="rounded border border-default px-2 py-1 flex items-center gap-2" data-testid={`witness-${w.id}`}>
                                            <span className="text-default font-medium">{w.operator_label}</span>
                                            <span className="text-muted">
                                                {w.kind} · {w.scope}
                                                {w.same_operator ? ' · same operator' : ''}
                                                {w.enabled ? '' : ' · disabled'}
                                                {w.pinned_key ? '' : ' · no pinned key'}
                                            </span>
                                            {latest ? (
                                                <span
                                                    className={`px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase ${chipFor(RECEIPT_VERDICT_CHIP, latest.verdict)}`}
                                                    title={`latest receipt at tree size ${latest.treeSize}${latest.error ? ` — ${latest.error}` : ''}`}
                                                >
                                                    {receiptVerdictLabel(latest.verdict)}
                                                </span>
                                            ) : (
                                                <span className="text-muted">no receipt among the latest heads</span>
                                            )}
                                        </li>
                                    );
                                })}
                            </ul>
                        )}
                    </div>

                    <div className={`${TABLE_CONTAINER} mx-4 mb-4`}>
                        <table className="w-full text-xs">
                            <thead className={TABLE_THEAD}>
                                <tr>
                                    <th className={TABLE_TH}>Size</th>
                                    <th className={TABLE_TH}>Root</th>
                                    <th className={TABLE_TH}>Issued</th>
                                    <th className={TABLE_TH}>Grade</th>
                                    <th className={TABLE_TH}>Receipts</th>
                                </tr>
                            </thead>
                            <tbody className={TABLE_TBODY}>
                                {heads.length === 0 && (
                                    <tr>
                                        <td colSpan={5} className="px-4 py-4 text-muted" data-testid="log-panel-no-heads">
                                            No tree head sealed yet.
                                        </td>
                                    </tr>
                                )}
                                {heads.map((h) => (
                                    <tr key={h.id} data-testid={`tree-head-${h.id}`}>
                                        <td className="px-4 py-2 text-default">{h.tree_size.toLocaleString()}</td>
                                        <td className="px-4 py-2 font-mono text-default" title={h.root}>{shortHash(h.root)}</td>
                                        <td className="px-4 py-2 text-secondary">{h.issued_at ? new Date(h.issued_at).toLocaleString() : '—'}</td>
                                        <td className="px-4 py-2">
                                            <span className={`px-2 py-0.5 rounded-full font-medium ${chipFor(HEAD_GRADE_CHIP, h.grade)}`}>
                                                {headGradeLabel(h.grade)}
                                            </span>
                                        </td>
                                        <td className="px-4 py-2">
                                            {(h.receipts ?? []).length === 0 ? (
                                                <span className="text-muted">none</span>
                                            ) : (
                                                <div className="flex flex-wrap gap-1">
                                                    {(h.receipts ?? []).map((r) => (
                                                        <span
                                                            key={r.id}
                                                            className={`px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase ${chipFor(RECEIPT_VERDICT_CHIP, r.verdict)}`}
                                                            title={`${r.kind}${r.error ? ` — ${r.error}` : ''}`}
                                                        >
                                                            {r.kind} · {receiptVerdictLabel(r.verdict)}
                                                        </span>
                                                    ))}
                                                </div>
                                            )}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                        {headsTotal > heads.length && (
                            <p className="px-4 py-2 text-xs text-muted border-t border-default">
                                Showing the latest {heads.length} of {headsTotal} heads.
                            </p>
                        )}
                    </div>
                </>
            )}
        </section>
    );
}

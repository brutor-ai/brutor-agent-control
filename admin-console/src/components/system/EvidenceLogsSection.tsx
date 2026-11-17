/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * System → Audit Chains → tenant evidence logs.
 *
 * The row chain above proves the audit rows were not rewritten; the tenant
 * evidence logs are the other half — one RFC 6962 tree per tenant whose
 * signed heads are sent to witnesses. Each head carries a grade:
 * standalone (no witness receipt), self-witnessed (only a same-operator
 * witness), witnessed (one independent receipt), plural (two or more).
 * A timestamp dates a head and never raises its grade.
 *
 * A head left standalone or self-witnessed for more than an hour is
 * highlighted as a finding — something to look at (a witness outage, no
 * witness configured) — and is deliberately not phrased as a verdict on
 * the records the head covers.
 */

import { useCallback, useState } from 'react';
import { ChevronDown, ChevronRight, Loader2, ScrollText } from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { useKeyedLoad } from '../../hooks/useKeyedLoad';
import { errorMessage } from '../../lib/narrow';
import {
    HEAD_GRADE_CHIP,
    RECEIPT_VERDICT_CHIP,
    receiptVerdictLabel,
    chipFor,
    headGradeLabel,
    shortHash,
    type EvidenceLogItem,
    type EvidenceTreeHead,
} from '../../lib/complianceEvidenceTypes';
import { isUnwitnessedFinding } from '../../lib/evidenceSurfaceTypes';
import { HelpTip } from '../ui/HelpTip';

const HEAD_PAGE = 20;

function hoursSince(iso: string | null | undefined, now: number): string {
    if (!iso) return '';
    const h = (now - Date.parse(iso)) / 3_600_000;
    return h >= 48 ? `${Math.floor(h / 24)} days` : `${Math.floor(h)} h`;
}

function LogHeads({ log }: { log: EvidenceLogItem }) {
    const { gatewayClient } = useGateway();
    const loader = useCallback(
        () => gatewayClient.listEvidenceLogTreeHeads(log.log_id, { tenantId: log.tenant_id, limit: HEAD_PAGE }),
        [gatewayClient, log.log_id, log.tenant_id],
    );
    const { data, error, loading } = useKeyedLoad(`heads:${log.log_id}`, loader, 'Failed to load tree heads');
    const now = Date.now();

    if (loading && !data) {
        return (
            <p className="text-xs text-muted flex items-center gap-1 p-2">
                <Loader2 className="w-3 h-3 animate-spin" /> Loading heads…
            </p>
        );
    }
    if (error) {
        return (
            <p className="text-xs text-red-700 p-2" data-testid={`evidence-heads-error-${log.log_id}`}>
                Could not load heads: {error}. This says nothing about the log itself — no check ran.
            </p>
        );
    }
    const heads: EvidenceTreeHead[] = data?.items ?? [];
    if (heads.length === 0) {
        return (
            <p className="text-xs text-muted p-2" data-testid={`evidence-heads-empty-${log.log_id}`}>
                No tree heads yet — the core issues one every 100 entries or 15 minutes after the first uncovered entry.
            </p>
        );
    }
    return (
        <div className="space-y-1.5 p-2" data-testid={`evidence-heads-${log.log_id}`}>
            {heads.map((h) => {
                const finding = isUnwitnessedFinding(h.grade, h.issued_at, now);
                const receipts = h.receipts ?? [];
                return (
                    <div
                        key={h.id}
                        className={`rounded border p-2 text-xs ${finding ? 'border-amber-300 bg-amber-50 dark:bg-amber-950/30' : 'border-default bg-surface'}`}
                        data-testid={`evidence-head-${h.id}`}
                    >
                        <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5">
                            <span className="font-mono text-default">size {h.tree_size.toLocaleString()}</span>
                            <span className="font-mono text-muted" title={h.root}>
                                root {shortHash(h.root, 12)}
                            </span>
                            <span className="text-secondary">{h.issued_at ? new Date(h.issued_at).toLocaleString() : '—'}</span>
                            <span
                                className={`px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase ${chipFor(HEAD_GRADE_CHIP, h.grade)}`}
                                data-testid={`evidence-head-grade-${h.id}`}
                            >
                                {headGradeLabel(h.grade)}
                            </span>
                        </div>
                        {finding && (
                            <p className="mt-1 text-amber-800" data-testid={`evidence-head-finding-${h.id}`}>
                                Finding: no independent witness receipt {hoursSince(h.issued_at, now)} after issue. Check the
                                witnesses and their reachability; the records under this head are unaffected.
                            </p>
                        )}
                        {receipts.length > 0 ? (
                            <ul className="mt-1 space-y-0.5" data-testid={`evidence-head-receipts-${h.id}`}>
                                {receipts.map((r) => (
                                    <li key={r.id} className="flex flex-wrap items-center gap-2 text-secondary">
                                        <span className="font-mono">{r.witness_id ?? 'unknown witness'}</span>
                                        <span className="text-muted">{r.kind}</span>
                                        <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase ${chipFor(RECEIPT_VERDICT_CHIP, r.verdict)}`}>
                                            {receiptVerdictLabel(r.verdict)}
                                        </span>
                                        {r.verified_at && <span className="text-muted">checked {new Date(r.verified_at).toLocaleString()}</span>}
                                        {r.error && <span className="text-red-700">{r.error}</span>}
                                    </li>
                                ))}
                            </ul>
                        ) : (
                            <p className="mt-1 text-muted">No receipts.</p>
                        )}
                    </div>
                );
            })}
            {(data?.total ?? 0) > heads.length && (
                <p className="text-[11px] text-muted">
                    Latest {heads.length} of {data?.total} heads.
                </p>
            )}
        </div>
    );
}

export default function EvidenceLogsSection() {
    const { gatewayClient } = useGateway();
    const [open, setOpen] = useState<Set<string>>(new Set());
    const loader = useCallback(() => gatewayClient.listEvidenceLogs(), [gatewayClient]);
    const { data, error, loading } = useKeyedLoad('evidence-logs', loader, 'Failed to list evidence logs');
    const logs = data?.items ?? null;

    const toggle = (id: string) =>
        setOpen((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });

    return (
        <div className="space-y-3" data-testid="evidence-logs-section">
            <div>
                <h3 className="text-lg font-semibold text-default flex items-center gap-2">
                    <ScrollText className="w-5 h-5 text-primary-600" /> Tenant evidence logs
                    <HelpTip text="One transparency log per tenant holds every sealed action record. Signed tree heads are registered with witnesses: standalone = no receipt; self-witnessed = only a same-operator witness; witnessed = one independent receipt checked against its pinned key; plural = two or more. Heads unwitnessed for over an hour are highlighted as findings, not as verdicts." />
                </h3>
                <p className="text-sm text-secondary mt-1 max-w-3xl">
                    The sealed-record logs behind the Evidence Ledger, with each head&apos;s witness grade and the receipts
                    per witness.
                </p>
            </div>

            {loading && !data && !error && (
                <p className="text-sm text-muted flex items-center gap-2">
                    <Loader2 className="w-4 h-4 animate-spin" /> Loading evidence logs…
                </p>
            )}
            {error && (
                <div className="p-3 rounded border border-red-200 bg-red-50 text-sm text-red-700" data-testid="evidence-logs-error">
                    Could not list evidence logs: {errorMessage(error)}
                </div>
            )}
            {logs?.length === 0 && (
                <p className="text-sm text-muted" data-testid="evidence-logs-empty">
                    No tenant evidence logs yet — the core creates a tenant&apos;s log with its first sealed record.
                </p>
            )}

            <div className="space-y-2">
                {logs?.map((log) => {
                    const expanded = open.has(log.log_id);
                    return (
                        <div key={log.log_id} className="border border-default rounded-lg bg-surface" data-testid={`evidence-log-${log.log_id}`}>
                            <button
                                type="button"
                                className="w-full flex flex-wrap items-center gap-x-4 gap-y-1 p-3 text-left text-xs"
                                aria-expanded={expanded}
                                onClick={() => toggle(log.log_id)}
                                data-testid={`evidence-log-toggle-${log.log_id}`}
                            >
                                {expanded ? <ChevronDown className="w-4 h-4 text-muted" /> : <ChevronRight className="w-4 h-4 text-muted" />}
                                <span className="font-medium text-default">{log.tenant_id}</span>
                                <span className="font-mono text-muted" title={log.log_id}>
                                    log {shortHash(log.log_id, 12)}
                                </span>
                                <span className="text-secondary">{log.tree_size.toLocaleString()} entries</span>
                                <span className="text-secondary">
                                    {log.heads_count} heads · {log.receipts_count} receipts
                                </span>
                                <span className="font-mono text-muted" title={log.kid ?? undefined}>
                                    key {shortHash(log.kid, 12)}
                                </span>
                                <span className="flex-1" />
                                {log.latest_grade ? (
                                    <span
                                        className={`px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase ${chipFor(HEAD_GRADE_CHIP, log.latest_grade)}`}
                                        data-testid={`evidence-log-grade-${log.log_id}`}
                                    >
                                        latest head: {headGradeLabel(log.latest_grade)}
                                    </span>
                                ) : (
                                    <span className="text-muted">no head yet</span>
                                )}
                                {isUnwitnessedFinding(log.last_tree_head?.grade, log.last_tree_head?.issued_at) && (
                                    <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-amber-100 text-amber-800" data-testid={`evidence-log-finding-${log.log_id}`}>
                                        unwitnessed &gt; 1 h
                                    </span>
                                )}
                            </button>
                            {expanded && (
                                <div className="border-t border-default">
                                    <LogHeads log={log} />
                                </div>
                            )}
                        </div>
                    );
                })}
            </div>
        </div>
    );
}

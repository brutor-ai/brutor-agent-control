/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useCallback, useEffect, useState } from 'react';
import { ShieldCheck, ShieldAlert, ShieldQuestion, Loader2 } from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import type { AuditChainReport } from '../../lib/types';

/**
 * Audit hash-chain verification.
 *
 * Every `proxy_logs` row is hash-chained to its predecessor and batches are
 * signed into `audit_chain_checkpoints`. That makes the records tamper-
 * *resistant*; this pane is what makes them tamper-**evident** — without
 * something an operator can run, the property exists but nobody can
 * demonstrate it, which is the half that closes an audit.
 *
 * The check runs in the Rust proxy, because it recomputes the canonical row
 * bytes and a second implementation of that format would drift.
 */

/** How each verdict should read. `verified` is the only one asserting integrity. */
const VERDICTS: Record<
    AuditChainReport['verdict'],
    { label: string; tone: string; icon: React.ElementType; blurb: string }
> = {
    verified: {
        label: 'Verified',
        tone: 'text-green-700 bg-green-50 border-green-200',
        icon: ShieldCheck,
        blurb: 'Every hash links, and a checkpoint signed by a trusted key covers them.',
    },
    unattested: {
        label: 'Unattested',
        tone: 'text-yellow-700 bg-yellow-50 border-yellow-200',
        icon: ShieldQuestion,
        blurb:
            'Hashes link, but nothing signed them — so the records are internally ' +
            'consistent and could still have been rewritten wholesale. Configure ' +
            'BRUTOR_AUDIT_SIGNING_KEY on the proxy to close this.',
    },
    gap: {
        label: 'Rows deleted',
        tone: 'text-orange-700 bg-orange-50 border-orange-200',
        icon: ShieldAlert,
        blurb: 'Sequence numbers are missing. Rows that were written are no longer present.',
    },
    broken: {
        label: 'Tampered',
        tone: 'text-red-700 bg-red-50 border-red-200',
        icon: ShieldAlert,
        blurb: 'A row no longer hashes to its stored value. Its contents changed after it was written.',
    },
    forged: {
        label: 'Forged attestation',
        tone: 'text-red-800 bg-red-100 border-red-300',
        icon: ShieldAlert,
        blurb:
            'A checkpoint signature is valid but was made with a key this deployment ' +
            'never trusted. Someone produced an attestation we did not authorise.',
    },
    unchained: {
        label: 'Not chained',
        tone: 'text-secondary bg-surface border-default',
        icon: ShieldQuestion,
        blurb:
            'No rows carry chain columns here. Records written before tamper-evidence ' +
            'was enabled cannot be verified — which is not the same as failing.',
    },
};

const SystemChainsPane: React.FC = () => {
    const { gatewayClient } = useGateway();

    const [chains, setChains] = useState<string[] | null>(null);
    const [listError, setListError] = useState<string | null>(null);
    const [busy, setBusy] = useState<string | null>(null);
    const [reports, setReports] = useState<Record<string, AuditChainReport>>({});
    const [errors, setErrors] = useState<Record<string, string>>({});

    const load = useCallback(async () => {
        try {
            const res = await gatewayClient.listAuditChains(100);
            setChains(res.chains);
            setListError(null);
        } catch (e) {
            setListError(e instanceof Error ? e.message : String(e));
            setChains([]);
        }
    }, [gatewayClient]);

    useEffect(() => {
        void load();
        // Mount-only, matching the other System panes. The chain list is
        // refreshed by explicit operator action, and `gatewayClient` identity
        // is not guaranteed stable across renders — depending on it here would
        // re-fetch on every render.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const verify = async (chainId: string) => {
        setBusy(chainId);
        setErrors(prev => {
            const next = { ...prev };
            delete next[chainId];
            return next;
        });
        try {
            const report = await gatewayClient.verifyAuditChain(chainId);
            setReports(prev => ({ ...prev, [chainId]: report }));
        } catch (e) {
            // A failure to check is never a clean bill of health, so it is
            // shown as its own state rather than leaving the last verdict up.
            setErrors(prev => ({
                ...prev,
                [chainId]: e instanceof Error ? e.message : String(e),
            }));
            setReports(prev => {
                const next = { ...prev };
                delete next[chainId];
                return next;
            });
        } finally {
            setBusy(null);
        }
    };

    return (
        <div className="space-y-4" data-testid="system-chains-pane">
            <div>
                <h3 className="text-lg font-semibold text-default">Audit Chains</h3>
                <p className="text-sm text-secondary mt-1 max-w-3xl">
                    Each proxy process writes its audit records as a hash chain and signs
                    batches of them. Verifying recomputes every row&apos;s hash, checks it
                    against its predecessor, reports missing sequence numbers, and validates
                    the signatures — so a change to the records after the fact is detectable
                    rather than invisible.
                </p>
            </div>

            {listError && (
                <div className="p-3 rounded border border-red-200 bg-red-50 text-sm text-red-700">
                    Could not list chains: {listError}
                    <div className="text-xs mt-1 text-red-600">
                        This says nothing about the integrity of the records — no check ran.
                    </div>
                </div>
            )}

            {chains === null && (
                <div className="text-sm text-muted flex items-center gap-2">
                    <Loader2 className="w-4 h-4 animate-spin" /> Loading chains…
                </div>
            )}

            {chains?.length === 0 && !listError && (
                <div className="text-sm text-muted">
                    No chained records yet. Chains appear once the proxy has written audit
                    rows; a restart begins a new one, which is expected.
                </div>
            )}

            <div className="space-y-3">
                {chains?.map(chainId => {
                    const report = reports[chainId];
                    const err = errors[chainId];
                    const meta = report ? VERDICTS[report.verdict] : null;
                    const Icon = meta?.icon;

                    return (
                        <div
                            key={chainId}
                            className="border border-default rounded-lg p-3 bg-surface"
                            data-testid={`chain-${chainId}`}
                        >
                            <div className="flex items-center justify-between gap-3">
                                <span className="font-mono text-xs break-all">{chainId}</span>
                                <button
                                    onClick={() => void verify(chainId)}
                                    disabled={busy === chainId}
                                    className="px-3 py-1 text-xs rounded border border-strong hover:bg-surface-alt disabled:opacity-50 shrink-0"
                                >
                                    {busy === chainId ? 'Verifying…' : 'Verify'}
                                </button>
                            </div>

                            {err && (
                                <div className="mt-2 text-xs text-red-700">
                                    Verification could not run: {err}
                                </div>
                            )}

                            {report && meta && (
                                <div className={`mt-2 p-2 rounded border text-xs ${meta.tone}`}>
                                    <div className="flex items-center gap-1.5 font-semibold">
                                        {Icon && <Icon className="w-4 h-4" />}
                                        {meta.label}
                                    </div>
                                    <div className="mt-1">{meta.blurb}</div>

                                    <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-0.5 font-mono text-[11px]">
                                        <span>rows checked</span>
                                        <span className="text-right">{report.rows_checked}</span>
                                        {report.seq_start !== null && (
                                            <>
                                                <span>sequence</span>
                                                <span className="text-right">
                                                    {report.seq_start}–{report.seq_end}
                                                </span>
                                            </>
                                        )}
                                        <span>checkpoints valid</span>
                                        <span className="text-right">
                                            {report.checkpoints_valid} / {report.checkpoints_checked}
                                        </span>
                                        {report.checkpoints_untrusted > 0 && (
                                            <>
                                                <span className="text-red-800">untrusted keys</span>
                                                <span className="text-right text-red-800">
                                                    {report.checkpoints_untrusted}
                                                </span>
                                            </>
                                        )}
                                        {report.first_bad_seq !== null && (
                                            <>
                                                <span>first bad row</span>
                                                <span className="text-right">
                                                    #{report.first_bad_seq}
                                                </span>
                                            </>
                                        )}
                                        {report.gaps.length > 0 && (
                                            <>
                                                <span>gaps</span>
                                                <span className="text-right">
                                                    {report.gaps
                                                        .map(([a, b]) => `${a}→${b}`)
                                                        .join(', ')}
                                                </span>
                                            </>
                                        )}
                                    </div>

                                    {report.truncated && (
                                        <div className="mt-2 pt-2 border-t border-current/20">
                                            Scan stopped at the row limit — this verdict describes
                                            the range examined, not the whole chain.
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>
                    );
                })}
            </div>
        </div>
    );
};

export default SystemChainsPane;

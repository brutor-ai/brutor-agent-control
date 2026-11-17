/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * One sealed action record, in one window.
 *
 * Shared by the Evidence Ledger (row click) and Run details ("View
 * record"), so a record reads the same wherever it is opened from. Shows
 * the record JSON, the Signed Statement (base64, copyable), where the
 * record sits in the tenant log, its RFC 6962 inclusion proof, and a
 * Verify button that asks the core to recompute the record's id, check the
 * signature and run the semantic rules. Findings are grouped by severity:
 * an error gates the result; warnings and infos do not.
 *
 * Wording discipline: a good result is "recomputed" and
 * "consistent", never one of the words the vocabulary lint bans.
 */

import { useCallback, useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { Check, Copy, Loader2, ShieldCheck, X } from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { useKeyedLoad } from '../../hooks/useKeyedLoad';
import { errorMessage } from '../../lib/narrow';
import { chipFor, shortHash, VERDICT_CHIP, type EvidenceVerifyResult } from '../../lib/complianceEvidenceTypes';
import { VerifyFindings } from './ComplianceUi';
import { VERIFY_OUTCOME_CLASS, VERIFY_OUTCOME_TITLE, verifyOutcome } from './complianceFormat';

interface RecordDialogProps {
    recordId: string;
    onClose: () => void;
}

export default function RecordDialog({ recordId, onClose }: RecordDialogProps) {
    const { gatewayClient } = useGateway();
    const [copied, setCopied] = useState(false);
    const [verifying, setVerifying] = useState(false);
    const [verify, setVerify] = useState<EvidenceVerifyResult | null>(null);
    const [verifyError, setVerifyError] = useState('');

    useEffect(() => {
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') onClose();
        };
        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [onClose]);

    const loadDetail = useCallback(() => gatewayClient.getEvidenceRecord(recordId), [gatewayClient, recordId]);
    const loadProof = useCallback(() => gatewayClient.getEvidenceRecordProof(recordId), [gatewayClient, recordId]);
    const { data: detail, error } = useKeyedLoad(recordId, loadDetail, 'Failed to load the record');
    // A record that is not yet in the tree has no proof; that is a state the
    // section reports, not a failure of the dialog.
    const { data: proof, error: proofError } = useKeyedLoad(recordId, loadProof, 'No inclusion proof is available');

    const copyStatement = () => {
        if (!detail?.statement_b64) return;
        void (async () => {
            try {
                await navigator.clipboard.writeText(detail.statement_b64 ?? '');
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
            } catch {
                // Clipboard denied; the statement is on screen either way.
            }
        })();
    };

    const runVerify = useCallback(async () => {
        setVerifying(true);
        setVerifyError('');
        try {
            setVerify(await gatewayClient.verifyEvidenceRecord(recordId));
        } catch (e: unknown) {
            setVerifyError(errorMessage(e, 'The verifier could not be reached'));
        } finally {
            setVerifying(false);
        }
    }, [gatewayClient, recordId]);

    const outcome = verify ? verifyOutcome(verify) : null;

    return createPortal(
        <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
            role="dialog"
            aria-modal="true"
            aria-label={`Record ${recordId}`}
            data-testid="record-dialog"
            onMouseDown={(e) => {
                if (e.target === e.currentTarget) onClose();
            }}
        >
            <div className="bg-surface border border-default rounded-xl shadow-xl w-full max-w-3xl max-h-[85vh] flex flex-col">
                <div className="flex items-center justify-between gap-3 px-5 py-3 border-b border-default">
                    <div className="min-w-0">
                        <h2 className="text-sm font-semibold text-default">Sealed record</h2>
                        <p className="font-mono text-xs text-secondary truncate" title={recordId}>
                            {recordId}
                        </p>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                        {detail && (
                            <span
                                className={`px-2 py-0.5 rounded-full text-xs font-medium ${chipFor(VERDICT_CHIP, detail.verdict)}`}
                                data-testid="record-dialog-verdict"
                            >
                                {detail.verdict}
                            </span>
                        )}
                        <button
                            type="button"
                            onClick={() => void runVerify()}
                            disabled={verifying || !detail}
                            className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium border border-strong rounded-lg hover:bg-surface-hover disabled:opacity-50"
                            data-testid="record-dialog-verify"
                        >
                            {verifying ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <ShieldCheck className="w-3.5 h-3.5" />}
                            Verify
                        </button>
                        <button
                            type="button"
                            onClick={onClose}
                            aria-label="Close record"
                            className="p-1 rounded hover:bg-surface-raised text-muted"
                            data-testid="record-dialog-close"
                        >
                            <X className="w-5 h-5" />
                        </button>
                    </div>
                </div>

                <div className="overflow-y-auto px-5 py-4 text-sm space-y-4">
                    {error && (
                        <p className="text-xs text-red-700" data-testid="record-dialog-error">
                            {error}
                        </p>
                    )}
                    {!error && !detail && <p className="text-xs text-muted">Loading the record…</p>}

                    {detail && (
                        <>
                            <section data-testid="record-dialog-position">
                                <h3 className="text-xs font-semibold text-default mb-1">Log position</h3>
                                <dl className="grid grid-cols-[minmax(140px,1fr)_2fr] gap-x-4 gap-y-1 text-xs">
                                    <dt className="text-muted font-mono">log_id</dt>
                                    <dd className="text-default break-all">{detail.log.log_id}</dd>
                                    <dt className="text-muted font-mono">seq</dt>
                                    <dd className="text-default">{detail.log.seq}</dd>
                                    <dt className="text-muted font-mono">tree_size_now</dt>
                                    <dd className="text-default">{detail.log.tree_size_now}</dd>
                                    <dt className="text-muted font-mono">kid</dt>
                                    <dd className="text-default break-all">{detail.kid}</dd>
                                    {detail.chain && (
                                        <>
                                            <dt className="text-muted font-mono">chain</dt>
                                            <dd className="text-default break-all">{JSON.stringify(detail.chain)}</dd>
                                        </>
                                    )}
                                </dl>
                            </section>

                            <section data-testid="record-dialog-proof">
                                <h3 className="text-xs font-semibold text-default mb-1">Inclusion proof</h3>
                                {proof ? (
                                    <dl className="grid grid-cols-[minmax(140px,1fr)_2fr] gap-x-4 gap-y-1 text-xs">
                                        <dt className="text-muted font-mono">leaf_index</dt>
                                        <dd className="text-default">{proof.leaf_index}</dd>
                                        <dt className="text-muted font-mono">tree_size</dt>
                                        <dd className="text-default">{proof.tree_size}</dd>
                                        <dt className="text-muted font-mono">root</dt>
                                        <dd className="text-default font-mono break-all" title={proof.root}>{proof.root}</dd>
                                        <dt className="text-muted font-mono">leaf_hash</dt>
                                        <dd className="text-default font-mono break-all">{proof.leaf_hash}</dd>
                                        <dt className="text-muted font-mono">path</dt>
                                        <dd className="text-default font-mono break-all">
                                            {proof.path.length === 0 ? '(single leaf)' : proof.path.map((p) => shortHash(p, 16)).join(' → ')}
                                        </dd>
                                    </dl>
                                ) : (
                                    <p className="text-xs text-muted" data-testid="record-dialog-proof-missing">
                                        {proofError || 'Computing the proof…'}
                                    </p>
                                )}
                            </section>

                            <section data-testid="record-dialog-statement">
                                <div className="flex items-center justify-between mb-1">
                                    <h3 className="text-xs font-semibold text-default">Signed Statement (base64)</h3>
                                    <button
                                        type="button"
                                        onClick={copyStatement}
                                        disabled={!detail.statement_b64}
                                        className="inline-flex items-center gap-1 px-2 py-0.5 text-xs border border-strong rounded hover:bg-surface-hover disabled:opacity-50"
                                        data-testid="record-dialog-copy-statement"
                                    >
                                        {copied ? <Check className="w-3 h-3 text-green-600" /> : <Copy className="w-3 h-3" />}
                                        Copy
                                    </button>
                                </div>
                                <pre className="text-[11px] font-mono whitespace-pre-wrap break-all bg-surface-alt border border-default rounded p-2 max-h-24 overflow-y-auto">
                                    {detail.statement_b64 ?? '— no statement stored —'}
                                </pre>
                            </section>

                            {(verify || verifyError) && (
                                <section
                                    data-testid="record-dialog-verify-result"
                                    className={`rounded-lg border p-3 ${
                                        outcome ? VERIFY_OUTCOME_CLASS[outcome] : 'border-red-300 bg-red-50'
                                    }`}
                                >
                                    <h3 className="text-xs font-semibold text-default mb-1">
                                        {outcome ? VERIFY_OUTCOME_TITLE[outcome] : 'Verifier unreachable'}
                                    </h3>
                                    {verifyError && <p className="text-xs text-red-800">{verifyError}</p>}
                                    {verify && (
                                        <>
                                            <p className="text-xs text-secondary">
                                                Statement signature{' '}
                                                {verify.statement?.verified === true
                                                    ? 'recomputed under the producer key'
                                                    : verify.statement?.verified === false
                                                      ? `not recomputed${verify.statement?.error ? ` — ${verify.statement.error}` : ''}`
                                                      : 'not checked'}
                                                {verify.statement?.kid ? ` · kid ${verify.statement.kid}` : ''}
                                                {verify.effect_mode ? ` · effect mode ${verify.effect_mode}` : ''}
                                            </p>
                                            <VerifyFindings findings={verify.findings} testIdPrefix="record-dialog" />
                                        </>
                                    )}
                                </section>
                            )}

                            <section data-testid="record-dialog-json">
                                <h3 className="text-xs font-semibold text-default mb-1">Record</h3>
                                <pre className="text-xs font-mono whitespace-pre-wrap break-all bg-surface-alt border border-default rounded p-3">
                                    {JSON.stringify(detail.record, null, 2)}
                                </pre>
                            </section>
                        </>
                    )}
                </div>
            </div>
        </div>,
        document.body,
    );
}

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Verify panel: ask the core's verifier — through the
 * control plane — to recompute a record and check its Signed Statement,
 * or to verify a whole bundle. Three modes:
 *
 *   * a stored record by id       → `POST /evidence/verify {record_id}`
 *   * a record and statement pasted inline
 *                                  → `POST /evidence/verify {record, statement_b64}`
 *   * a bundle pasted as JSON, with the digest it claims
 *                                  → `POST /evidence/bundles/verify {bundle, bundle_digest}`
 *
 * Tree heads and receipts are verified inside a bundle; the control plane
 * exposes no standalone tree-head or receipt check, and the panel says so
 * instead of pretending. A verifier that could not run is a state of the
 * tool (`verifier_unavailable`), shown apart from any result — never as a
 * clean one. The offline verifier on the docs site does the same checks
 * without this deployment in the loop.
 */

import { useState } from 'react';
import { ExternalLink, Loader2, ShieldCheck } from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { errorMessage, isRecord, parseJson } from '../../lib/narrow';
import type { EvidenceVerifyResult } from '../../lib/complianceEvidenceTypes';
import type { BundleVerifyResult } from '../../lib/evidenceSurfaceTypes';
import { Section, VerifyFindings } from './ComplianceUi';
import { BUTTON_PRIMARY, INPUT_CLASS, OFFLINE_VERIFIER_URL, VERIFY_OUTCOME_CLASS, VERIFY_OUTCOME_TITLE, verifyOutcome } from './complianceFormat';

type Mode = 'record' | 'inline' | 'bundle';

const MODES: { id: Mode; label: string }[] = [
    { id: 'record', label: 'Stored record' },
    { id: 'inline', label: 'Record + statement' },
    { id: 'bundle', label: 'Bundle' },
];

/** A pasted JSON object, or the reason it is not one. */
function parseObject(text: string, what: string): { value: Record<string, unknown> } | { error: string } {
    if (!text.trim()) return { error: `Paste the ${what} JSON.` };
    const value = parseJson(text);
    if (value === undefined) return { error: `Not valid JSON — the ${what} could not be parsed.` };
    if (!isRecord(value)) return { error: `The ${what} must be a JSON object.` };
    return { value };
}

type Outcome =
    | { kind: 'record'; result: EvidenceVerifyResult }
    | { kind: 'bundle'; result: BundleVerifyResult };

interface Props {
    /** A record id to prefill (a row's "Verify" shortcut). */
    initialRecordId?: string | null;
}

export default function VerifyPanel({ initialRecordId = null }: Props) {
    const { gatewayClient } = useGateway();
    const [mode, setMode] = useState<Mode>('record');
    const [recordId, setRecordId] = useState(initialRecordId ?? '');
    const [recordJson, setRecordJson] = useState('');
    const [statementB64, setStatementB64] = useState('');
    const [bundleJson, setBundleJson] = useState('');
    const [bundleDigest, setBundleDigest] = useState('');
    const [busy, setBusy] = useState(false);
    const [inputError, setInputError] = useState('');
    const [requestError, setRequestError] = useState('');
    const [outcome, setOutcome] = useState<Outcome | null>(null);

    const run = async () => {
        setInputError('');
        setRequestError('');
        setOutcome(null);
        try {
            if (mode === 'record') {
                const id = recordId.trim();
                if (!id) {
                    setInputError('Enter a record id.');
                    return;
                }
                setBusy(true);
                setOutcome({ kind: 'record', result: await gatewayClient.verifyEvidenceRecord(id) });
            } else if (mode === 'inline') {
                const record = parseObject(recordJson, 'record');
                if ('error' in record) {
                    setInputError(record.error);
                    return;
                }
                setBusy(true);
                setOutcome({
                    kind: 'record',
                    result: await gatewayClient.verifyEvidenceInline(record.value, statementB64.trim() || null),
                });
            } else {
                const bundle = parseObject(bundleJson, 'bundle');
                if ('error' in bundle) {
                    setInputError(bundle.error);
                    return;
                }
                setBusy(true);
                setOutcome({
                    kind: 'bundle',
                    result: await gatewayClient.verifyEvidenceBundle(bundle.value, bundleDigest.trim() || null),
                });
            }
        } catch (e: unknown) {
            setRequestError(errorMessage(e, 'The verifier could not be reached'));
        } finally {
            setBusy(false);
        }
    };

    const verdict = outcome ? verifyOutcome(outcome.result) : null;

    return (
        <Section
            title="Verify"
            subtitle="Recompute a record's id, check its Signed Statement and run the semantic rules — or verify a whole bundle (records, proofs, tree heads, receipts). Standalone tree-head and receipt checks are not offered by the control plane; verify them inside a bundle, or offline."
            actions={
                <a href={OFFLINE_VERIFIER_URL} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-xs text-primary-600 hover:underline" data-testid="verify-offline-link">
                    <ExternalLink className="w-3.5 h-3.5" /> Verify offline
                </a>
            }
            testId="verify-panel"
        >
            <div className="space-y-3">
                <div className="inline-flex rounded-md border border-default overflow-hidden" role="tablist" aria-label="Verify mode">
                    {MODES.map((m) => (
                        <button
                            key={m.id}
                            type="button"
                            role="tab"
                            aria-selected={mode === m.id}
                            onClick={() => {
                                setMode(m.id);
                                setOutcome(null);
                                setInputError('');
                                setRequestError('');
                            }}
                            className={`px-2.5 py-1.5 text-xs ${mode === m.id ? 'bg-primary-50 text-primary-600 dark:bg-primary-900/20' : 'text-secondary hover:bg-surface-hover'}`}
                            data-testid={`verify-mode-${m.id}`}
                        >
                            {m.label}
                        </button>
                    ))}
                </div>

                {mode === 'record' && (
                    <label className="block text-xs">
                        <span className="text-muted uppercase tracking-wide">Record id</span>
                        <input
                            type="text"
                            value={recordId}
                            onChange={(e) => setRecordId(e.target.value)}
                            className={`${INPUT_CLASS} block w-full font-mono mt-1`}
                            placeholder="sha256 record id"
                            data-testid="verify-record-id"
                        />
                    </label>
                )}
                {mode === 'inline' && (
                    <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                        <label className="block text-xs">
                            <span className="text-muted uppercase tracking-wide">Record (JSON)</span>
                            <textarea
                                value={recordJson}
                                onChange={(e) => setRecordJson(e.target.value)}
                                className={`${INPUT_CLASS} block w-full font-mono mt-1 h-32`}
                                placeholder='{"type": "ai.brutor/action-record/v1", …}'
                                data-testid="verify-record-json"
                            />
                        </label>
                        <label className="block text-xs">
                            <span className="text-muted uppercase tracking-wide">Signed Statement (base64, optional)</span>
                            <textarea
                                value={statementB64}
                                onChange={(e) => setStatementB64(e.target.value)}
                                className={`${INPUT_CLASS} block w-full font-mono mt-1 h-32`}
                                data-testid="verify-statement-b64"
                            />
                        </label>
                    </div>
                )}
                {mode === 'bundle' && (
                    <div className="space-y-2">
                        <label className="block text-xs">
                            <span className="text-muted uppercase tracking-wide">Bundle (JSON)</span>
                            <textarea
                                value={bundleJson}
                                onChange={(e) => setBundleJson(e.target.value)}
                                className={`${INPUT_CLASS} block w-full font-mono mt-1 h-32`}
                                placeholder='{"type": "ai.brutor/evidence-bundle/v1", …}'
                                data-testid="verify-bundle-json"
                            />
                        </label>
                        <label className="block text-xs">
                            <span className="text-muted uppercase tracking-wide">Expected digest (optional)</span>
                            <input
                                type="text"
                                value={bundleDigest}
                                onChange={(e) => setBundleDigest(e.target.value)}
                                className={`${INPUT_CLASS} block w-full font-mono mt-1`}
                                data-testid="verify-bundle-digest"
                            />
                        </label>
                    </div>
                )}

                <div className="flex items-center gap-3 flex-wrap">
                    <button type="button" className={BUTTON_PRIMARY} onClick={() => void run()} disabled={busy} data-testid="verify-run">
                        {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <ShieldCheck className="w-3.5 h-3.5" />}
                        Verify
                    </button>
                    {inputError && (
                        <span className="text-xs text-red-700" data-testid="verify-input-error">
                            {inputError}
                        </span>
                    )}
                </div>

                {requestError && (
                    <div className="rounded-lg border border-amber-300 bg-amber-50 p-3" data-testid="verify-result">
                        <h4 className="text-xs font-semibold text-default">Verifier unreachable — nothing was checked</h4>
                        <p className="text-xs text-amber-800">{requestError}</p>
                    </div>
                )}

                {outcome && verdict && (
                    <div className={`rounded-lg border p-3 ${VERIFY_OUTCOME_CLASS[verdict]}`} data-testid="verify-result" data-outcome={verdict}>
                        <h4 className="text-xs font-semibold text-default">{VERIFY_OUTCOME_TITLE[verdict]}</h4>
                        {outcome.kind === 'record' && (
                            <p className="text-xs text-secondary">
                                {outcome.result.record_id ? `record ${outcome.result.record_id} · ` : ''}
                                Statement signature{' '}
                                {outcome.result.statement?.verified === true
                                    ? 'recomputed under the producer key'
                                    : outcome.result.statement?.verified === false
                                      ? `not recomputed${outcome.result.statement?.error ? ` — ${outcome.result.statement.error}` : ''}`
                                      : 'not checked'}
                                {outcome.result.statement?.kid ? ` · kid ${outcome.result.statement.kid}` : ''}
                                {outcome.result.effect_mode ? ` · effect mode ${outcome.result.effect_mode}` : ''}
                            </p>
                        )}
                        {outcome.kind === 'bundle' && (
                            <p className="text-xs text-secondary" data-testid="verify-bundle-summary">
                                {outcome.result.bundle_digest ? `digest ${outcome.result.bundle_digest}` : 'no digest computed'}
                                {outcome.result.digest_matches === true
                                    ? ' · matches the expected digest'
                                    : outcome.result.digest_matches === false
                                      ? ' · does NOT match the expected digest'
                                      : ''}
                                {outcome.result.records_verified !== null ? ` · ${outcome.result.records_verified} records recomputed` : ''}
                                {outcome.result.proofs_verified !== null ? ` · ${outcome.result.proofs_verified} proofs` : ''}
                                {outcome.result.heads_verified !== null ? ` · ${outcome.result.heads_verified} heads` : ''}
                                {outcome.result.missing.length > 0 ? ` · ${outcome.result.missing.length} referenced record(s) missing` : ''}
                            </p>
                        )}
                        <VerifyFindings findings={outcome.result.findings} testIdPrefix="verify" />
                    </div>
                )}
            </div>
        </Section>
    );
}

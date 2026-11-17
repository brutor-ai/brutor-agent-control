/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Run details → sealed steps: every action record the run
 * produced, in log order, with its verdict (all fourteen, `planned`
 * included), derived effect mode and chain links — planned → confirms,
 * awaiting_human → supersedes / expired — and "Bundle / Verify this run".
 *
 * The bundle is built by the core (selector `root_task_id`) through the CP,
 * and verified by the core's verifier. An open half of a chain — a planned
 * record nothing confirms, an approval nothing resolves — is shown as
 * such: it is the may/did gap a crash mid-dispatch leaves, and hiding it
 * would make the run look more settled than the log says it is.
 */

import { useCallback, useMemo, useState } from 'react';
import { Download, FileArchive, Link2, Loader2, ScrollText, ShieldCheck } from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { useKeyedLoad } from '../../hooks/useKeyedLoad';
import { downloadJson } from '../../lib/downloadJson';
import { errorMessage } from '../../lib/narrow';
import { chipFor, shortHash, type EvidenceRecordSummary } from '../../lib/complianceEvidenceTypes';
import {
    STEP_VERDICT_CHIP,
    describeChainLink,
    effectModeLabel,
    openHalves,
    readChain,
    verdictLabel,
    type BundleBuildResult,
    type BundleVerifyResult,
} from '../../lib/evidenceSurfaceTypes';
import RecordDialog from '../compliance/RecordDialog';
import { TABLE_TBODY, TABLE_TH_TYPE, TABLE_THEAD } from '../ui/tableStyles';

interface Props {
    rootTaskId: string;
}

/** A run's steps; more than this is a ledger question, not a dialog one. */
const STEP_LIMIT = 200;

export default function RunEvidenceSection({ rootTaskId }: Props) {
    const { gatewayClient } = useGateway();
    const [openRecord, setOpenRecord] = useState<string | null>(null);
    const [bundle, setBundle] = useState<BundleBuildResult | null>(null);
    const [verify, setVerify] = useState<BundleVerifyResult | null>(null);
    const [busy, setBusy] = useState<'bundle' | 'verify' | null>(null);
    const [actionError, setActionError] = useState('');

    const loader = useCallback(async () => {
        const r = await gatewayClient.listEvidenceRecords({ root_task_id: rootTaskId, limit: STEP_LIMIT });
        // The API is newest first; a run reads oldest first.
        return { items: [...r.items].reverse(), total: r.total };
    }, [gatewayClient, rootTaskId]);
    const { data, error, loading } = useKeyedLoad(`run-evidence:${rootTaskId}`, loader, 'Failed to load sealed steps');
    const steps = useMemo(() => data?.items ?? [], [data]);

    const children = useMemo(() => {
        const m = new Map<string, EvidenceRecordSummary[]>();
        for (const s of steps) {
            const link = readChain(s.chain);
            if (link) m.set(link.parentRecordId, [...(m.get(link.parentRecordId) ?? []), s]);
        }
        return m;
    }, [steps]);
    const open = useMemo(() => openHalves(steps), [steps]);

    const buildBundle = async (): Promise<BundleBuildResult | null> => {
        setBusy('bundle');
        setActionError('');
        setVerify(null);
        try {
            const b = await gatewayClient.buildEvidenceBundle({ selector: { root_task_id: rootTaskId }, closure_depth: 1 });
            setBundle(b);
            return b;
        } catch (e: unknown) {
            setActionError(`Bundle failed: ${errorMessage(e)}`);
            return null;
        } finally {
            setBusy(null);
        }
    };

    const verifyRun = async () => {
        const b = bundle ?? (await buildBundle());
        if (!b?.bundle) return;
        setBusy('verify');
        setActionError('');
        try {
            setVerify(await gatewayClient.verifyEvidenceBundle(b.bundle, b.bundle_digest));
        } catch (e: unknown) {
            // A verifier that could not run is never a clean result.
            setActionError(`Verification could not run: ${errorMessage(e)}`);
            setVerify(null);
        } finally {
            setBusy(null);
        }
    };

    const byId = new Map(steps.map((s) => [s.record_id, s]));

    return (
        <section className="space-y-2" data-testid="run-evidence-section">
            <div className="flex items-center justify-between gap-2 flex-wrap">
                <h3 className="text-xs font-semibold text-default flex items-center gap-1">
                    <ScrollText className="w-3.5 h-3.5 text-primary-600" /> Sealed steps
                    {data && <span className="font-normal text-muted">({data.total})</span>}
                </h3>
                <div className="flex items-center gap-2">
                    <button
                        type="button"
                        onClick={() => void buildBundle()}
                        disabled={busy !== null || steps.length === 0}
                        className="inline-flex items-center gap-1 px-2 py-0.5 text-[11px] border border-strong rounded hover:bg-surface-hover disabled:opacity-50"
                        data-testid="run-evidence-bundle"
                    >
                        {busy === 'bundle' ? <Loader2 className="w-3 h-3 animate-spin" /> : <FileArchive className="w-3 h-3" />}
                        Bundle this run
                    </button>
                    <button
                        type="button"
                        onClick={() => void verifyRun()}
                        disabled={busy !== null || steps.length === 0}
                        className="inline-flex items-center gap-1 px-2 py-0.5 text-[11px] border border-strong rounded hover:bg-surface-hover disabled:opacity-50"
                        data-testid="run-evidence-verify"
                    >
                        {busy === 'verify' ? <Loader2 className="w-3 h-3 animate-spin" /> : <ShieldCheck className="w-3 h-3" />}
                        Verify this run
                    </button>
                </div>
            </div>

            {loading && !data && !error && <p className="text-xs text-muted">Loading sealed steps…</p>}
            {error && (
                <p className="text-xs text-red-700" data-testid="run-evidence-error">
                    {error}
                </p>
            )}
            {data && steps.length === 0 && (
                <p className="text-xs text-muted" data-testid="run-evidence-empty">
                    No sealed records carry this run&apos;s root task id. Runs of systems without evidence records, or
                    rows written before the signing key was set, have none.
                </p>
            )}
            {data && data.total > steps.length && (
                <p className="text-[11px] text-amber-800">
                    Showing the latest {steps.length} of {data.total} — the bundle carries them all.
                </p>
            )}

            {steps.length > 0 && (
                <div className="overflow-x-auto border border-default rounded">
                    <table className="w-full text-[11px]">
                        <thead className={TABLE_THEAD}>
                            <tr>
                                <th className={`${TABLE_TH_TYPE} text-left px-2 py-1`}>#</th>
                                <th className={`${TABLE_TH_TYPE} text-left px-2 py-1`}>Action</th>
                                <th className={`${TABLE_TH_TYPE} text-left px-2 py-1`}>Verdict</th>
                                <th className={`${TABLE_TH_TYPE} text-left px-2 py-1`}>Effect mode</th>
                                <th className={`${TABLE_TH_TYPE} text-left px-2 py-1`}>Chain</th>
                                <th className={`${TABLE_TH_TYPE} text-left px-2 py-1`}>Record</th>
                            </tr>
                        </thead>
                        <tbody className={TABLE_TBODY}>
                            {steps.map((s, i) => {
                                const link = readChain(s.chain);
                                const parent = link ? byId.get(link.parentRecordId) : undefined;
                                const kids = children.get(s.record_id) ?? [];
                                return (
                                    <tr key={s.record_id} data-testid={`run-step-${s.record_id}`}>
                                        <td className="px-2 py-1 text-muted">{i + 1}</td>
                                        <td className="px-2 py-1 font-mono break-all">
                                            {s.action_id ?? '—'}
                                            {s.timestamp && (
                                                <span className="block font-sans text-muted">{new Date(s.timestamp).toLocaleString()}</span>
                                            )}
                                        </td>
                                        <td className="px-2 py-1">
                                            <span
                                                className={`px-1.5 py-0.5 rounded font-semibold ${chipFor(STEP_VERDICT_CHIP, s.verdict)}`}
                                                data-testid={`run-step-verdict-${s.record_id}`}
                                            >
                                                {verdictLabel(s.verdict)}
                                            </span>
                                            {s.human_disposed && <span className="ml-1 text-muted">· human</span>}
                                        </td>
                                        <td className="px-2 py-1 text-secondary" data-testid={`run-step-mode-${s.record_id}`}>
                                            {effectModeLabel(s.effect_mode)}
                                        </td>
                                        <td className="px-2 py-1 text-secondary" data-testid={`run-step-chain-${s.record_id}`}>
                                            {link && (
                                                <span className="flex items-center gap-1">
                                                    <Link2 className="w-3 h-3" />
                                                    {describeChainLink(s.verdict, link.relation)}{' '}
                                                    <button
                                                        type="button"
                                                        className="font-mono text-primary-600 hover:underline"
                                                        onClick={() => setOpenRecord(link.parentRecordId)}
                                                        title={link.parentRecordId}
                                                    >
                                                        {parent ? `#${steps.indexOf(parent) + 1}` : shortHash(link.parentRecordId, 10)}
                                                    </button>
                                                </span>
                                            )}
                                            {kids.map((k) => (
                                                <span key={k.record_id} className="block">
                                                    {k.verdict === 'expired'
                                                        ? 'expired by'
                                                        : readChain(k.chain)?.relation === 'confirms'
                                                          ? 'confirmed by'
                                                          : 'superseded by'}{' '}
                                                    #{steps.indexOf(k) + 1}
                                                </span>
                                            ))}
                                            {open.has(s.record_id) && (
                                                <span className="block text-amber-800" data-testid={`run-step-open-${s.record_id}`}>
                                                    {s.verdict === 'planned'
                                                        ? 'open — no outcome record confirms it'
                                                        : 'open — no record resolves this approval'}
                                                </span>
                                            )}
                                            {!link && kids.length === 0 && !open.has(s.record_id) && '—'}
                                        </td>
                                        <td className="px-2 py-1">
                                            <button
                                                type="button"
                                                className="font-mono text-primary-600 hover:underline"
                                                onClick={() => setOpenRecord(s.record_id)}
                                                title={s.record_id}
                                                data-testid={`run-step-record-${s.record_id}`}
                                            >
                                                {shortHash(s.record_id, 12)}
                                            </button>
                                        </td>
                                    </tr>
                                );
                            })}
                        </tbody>
                    </table>
                </div>
            )}

            {actionError && (
                <p className="text-xs text-red-700" data-testid="run-evidence-action-error">
                    {actionError}
                </p>
            )}

            {bundle && (
                <div className="rounded border border-default bg-surface-alt p-2 text-[11px] space-y-0.5" data-testid="run-evidence-bundle-result">
                    <div className="flex items-center justify-between gap-2">
                        <span className="font-semibold text-default">Bundle {bundle.bundle_id ?? ''}</span>
                        {bundle.bundle && (
                            <button
                                type="button"
                                onClick={() => downloadJson(`evidence-bundle-${rootTaskId}.json`, bundle.bundle)}
                                className="inline-flex items-center gap-1 px-2 py-0.5 border border-strong rounded hover:bg-surface-hover"
                                data-testid="run-evidence-download"
                            >
                                <Download className="w-3 h-3" /> Download JSON
                            </button>
                        )}
                    </div>
                    <div className="font-mono break-all text-secondary">digest {bundle.bundle_digest ?? '—'}</div>
                    <div className="text-secondary">
                        {bundle.record_count} records
                        {bundle.record_id && <> · sealed as <span className="font-mono">{shortHash(bundle.record_id, 12)}</span></>}
                    </div>
                    {bundle.missing.length > 0 && (
                        <div className="text-amber-800">
                            Missing from the bundle ({bundle.missing.length}): {bundle.missing.map((m) => shortHash(m, 10)).join(', ')}
                        </div>
                    )}
                </div>
            )}

            {verify && (
                <div
                    className={`rounded border p-2 text-[11px] ${verify.ok ? 'border-green-300 bg-green-50 text-green-800' : 'border-red-300 bg-red-50 text-red-800'}`}
                    data-testid="run-evidence-verify-result"
                >
                    <div className="font-semibold">
                        {verify.ok ? 'No findings' : `${verify.findings.length} finding${verify.findings.length === 1 ? '' : 's'}`}
                        {verify.records_verified !== null && (
                            <span className="font-normal">
                                {' '}
                                — records {verify.records_verified}, proofs {verify.proofs_verified ?? 0}, heads {verify.heads_verified ?? 0} checked
                            </span>
                        )}
                    </div>
                    {verify.digest_matches === false && <div>The bundle digest does not match the one the core returned.</div>}
                    {verify.findings.map((f, i) => (
                        <div key={`${f.code}-${i}`}>
                            <span className="font-mono">{f.code}</span> ({f.severity}) {f.detail}
                        </div>
                    ))}
                </div>
            )}

            {openRecord && <RecordDialog recordId={openRecord} onClose={() => setOpenRecord(null)} />}
        </section>
    );
}

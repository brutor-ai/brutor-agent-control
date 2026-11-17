/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Bundle export: ask the core, through the control
 * plane, for an `ai.brutor/evidence-bundle/v1` over exactly one selector —
 * record ids, a run, or a system over a window — with the chain closure
 * to the given depth. Naming an audience seals a disclosure record (what
 * was handed to whom, which records, which fields withheld). The bundle
 * downloads as JSON with its digest stated beside it; the offline verifier
 * checks it without this deployment in the loop.
 */

import { useState } from 'react';
import { Download, ExternalLink, Loader2, Package } from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { errorMessage } from '../../lib/narrow';
import type { ComplianceFramework } from '../../lib/complianceEvidenceTypes';
import type { BundleBuildResult } from '../../lib/evidenceSurfaceTypes';
import { RecordLink, Section } from './ComplianceUi';
import { BUTTON_PRIMARY, BUTTON_SECONDARY, downloadJson, INPUT_CLASS, OFFLINE_VERIFIER_URL } from './complianceFormat';
import { buildBundleRequest, type BundleSelectorKind } from './ledgerHelpers';

type SelectorKind = BundleSelectorKind;

interface Props {
    systems: { id: string; name: string }[];
    frameworks: ComplianceFramework[];
    /** The console's selected framework — the default `framework_id`. */
    framework: string;
    /** Called after a disclosure was sealed so the list can reload. */
    onDisclosed?: () => void;
    onOpenRecord?: (recordId: string) => void;
    /** Prefill (a row's "Bundle" shortcut). */
    initialRecordIds?: string[];
}

export default function BundleExportPanel({ systems, frameworks, framework, onDisclosed, onOpenRecord, initialRecordIds = [] }: Props) {
    const { gatewayClient } = useGateway();
    const [kind, setKind] = useState<SelectorKind>('record_ids');
    const [recordIds, setRecordIds] = useState(initialRecordIds.join('\n'));
    const [rootTaskId, setRootTaskId] = useState('');
    const [aiSystemId, setAiSystemId] = useState('');
    const [from, setFrom] = useState('');
    const [to, setTo] = useState('');
    const [audience, setAudience] = useState('');
    const [frameworkId, setFrameworkId] = useState(framework);
    const [closureDepth, setClosureDepth] = useState('1');
    const [busy, setBusy] = useState(false);
    const [inputError, setInputError] = useState('');
    const [requestError, setRequestError] = useState('');
    const [result, setResult] = useState<BundleBuildResult | null>(null);

    const build = async () => {
        setInputError('');
        setRequestError('');
        const prepared = buildBundleRequest({
            kind,
            recordIds,
            rootTaskId,
            aiSystemId,
            from,
            to,
            audience,
            frameworkId,
            closureDepth: Number(closureDepth),
        });
        if ('error' in prepared) {
            setInputError(prepared.error);
            return;
        }
        setBusy(true);
        try {
            const built = await gatewayClient.buildEvidenceBundle(prepared.body);
            setResult(built);
            if (prepared.body.audience) onDisclosed?.();
        } catch (e: unknown) {
            setRequestError(errorMessage(e, 'The bundle could not be built'));
        } finally {
            setBusy(false);
        }
    };

    const download = () => {
        if (!result?.bundle) return;
        const stem = result.bundle_digest ? result.bundle_digest.slice(0, 16) : 'bundle';
        downloadJson(result.bundle, `evidence-bundle-${stem}.json`);
    };

    return (
        <Section
            title="Bundle export"
            subtitle="One selector per bundle. Records come with their inclusion proofs, the tree heads that cover them and the witnesses' receipts. Naming an audience seals a disclosure record."
            actions={
                <a href={OFFLINE_VERIFIER_URL} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-xs text-primary-600 hover:underline" data-testid="bundle-offline-link">
                    <ExternalLink className="w-3.5 h-3.5" /> Verify offline
                </a>
            }
            testId="bundle-export-panel"
        >
            <div className="space-y-3">
                <div className="flex flex-wrap gap-3 text-xs" role="radiogroup" aria-label="Selector">
                    {(
                        [
                            ['record_ids', 'Record ids'],
                            ['root_task_id', 'A run'],
                            ['ai_system_id', 'A system over a window'],
                        ] as [SelectorKind, string][]
                    ).map(([k, label]) => (
                        <label key={k} className="inline-flex items-center gap-1.5">
                            <input type="radio" name="bundle-selector" checked={kind === k} onChange={() => setKind(k)} data-testid={`bundle-selector-${k}`} />
                            {label}
                        </label>
                    ))}
                </div>

                {kind === 'record_ids' && (
                    <textarea
                        value={recordIds}
                        onChange={(e) => setRecordIds(e.target.value)}
                        className={`${INPUT_CLASS} block w-full font-mono h-24`}
                        placeholder="one record id per line"
                        aria-label="Record ids"
                        data-testid="bundle-record-ids"
                    />
                )}
                {kind === 'root_task_id' && (
                    <input
                        type="text"
                        value={rootTaskId}
                        onChange={(e) => setRootTaskId(e.target.value)}
                        className={`${INPUT_CLASS} block w-full font-mono`}
                        placeholder="run (root task) id"
                        aria-label="Run id"
                        data-testid="bundle-run-id"
                    />
                )}
                {kind === 'ai_system_id' && (
                    <div className="flex flex-wrap items-end gap-2">
                        <label className="block text-xs">
                            <span className="text-muted uppercase tracking-wide">System</span>
                            <select value={aiSystemId} onChange={(e) => setAiSystemId(e.target.value)} className={`${INPUT_CLASS} block`} data-testid="bundle-system">
                                <option value="">choose…</option>
                                {systems.map((s) => (
                                    <option key={s.id} value={s.id}>
                                        {s.name}
                                    </option>
                                ))}
                            </select>
                        </label>
                        <label className="block text-xs">
                            <span className="text-muted uppercase tracking-wide">From</span>
                            <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} className={`${INPUT_CLASS} block`} data-testid="bundle-from" />
                        </label>
                        <label className="block text-xs">
                            <span className="text-muted uppercase tracking-wide">To</span>
                            <input type="date" value={to} onChange={(e) => setTo(e.target.value)} className={`${INPUT_CLASS} block`} data-testid="bundle-to" />
                        </label>
                    </div>
                )}

                <div className="flex flex-wrap items-end gap-2">
                    <label className="block text-xs">
                        <span className="text-muted uppercase tracking-wide">Audience (optional ⇒ disclosure)</span>
                        <input
                            type="text"
                            value={audience}
                            onChange={(e) => setAudience(e.target.value)}
                            className={`${INPUT_CLASS} block w-48`}
                            placeholder="e.g. external auditor"
                            data-testid="bundle-audience"
                        />
                    </label>
                    <label className="block text-xs">
                        <span className="text-muted uppercase tracking-wide">Framework</span>
                        <select value={frameworkId} onChange={(e) => setFrameworkId(e.target.value)} className={`${INPUT_CLASS} block`} data-testid="bundle-framework">
                            <option value="">none</option>
                            {frameworks.map((f) => (
                                <option key={f.id} value={f.id}>
                                    {f.title}
                                </option>
                            ))}
                        </select>
                    </label>
                    <label className="block text-xs">
                        <span className="text-muted uppercase tracking-wide">Chain closure depth</span>
                        <input
                            type="number"
                            min={0}
                            max={8}
                            value={closureDepth}
                            onChange={(e) => setClosureDepth(e.target.value)}
                            className={`${INPUT_CLASS} block w-20`}
                            data-testid="bundle-depth"
                        />
                    </label>
                    <button type="button" className={BUTTON_PRIMARY} onClick={() => void build()} disabled={busy} data-testid="bundle-build">
                        {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Package className="w-3.5 h-3.5" />}
                        Build bundle
                    </button>
                    {inputError && (
                        <span className="text-xs text-red-700" data-testid="bundle-input-error">
                            {inputError}
                        </span>
                    )}
                </div>

                {requestError && (
                    <p className="text-xs text-red-800 bg-red-50 border border-red-300 rounded p-2" data-testid="bundle-error">
                        {requestError}
                    </p>
                )}

                {result && (
                    <div className="rounded-lg border border-default bg-surface-alt p-3 text-xs space-y-1.5" data-testid="bundle-result">
                        <p className="text-default">
                            Bundle {result.bundle_id ? <code className="font-mono">{result.bundle_id}</code> : 'built'} ·{' '}
                            {result.record_count} record{result.record_count === 1 ? '' : 's'}
                            {result.missing.length > 0 && (
                                <span className="text-amber-800"> · {result.missing.length} referenced record(s) not held — the manifest says which</span>
                            )}
                        </p>
                        <p className="text-secondary break-all">
                            digest{' '}
                            <code className="font-mono" data-testid="bundle-digest">
                                {result.bundle_digest ?? '— (not stated)'}
                            </code>
                        </p>
                        {result.record_id ? (
                            <p className="text-secondary">
                                Sealed as record {onOpenRecord ? <RecordLink recordId={result.record_id} onOpen={onOpenRecord} /> : <code className="font-mono">{result.record_id}</code>}
                                {audience.trim() ? ` · disclosed to ${audience.trim()}` : ''}
                            </p>
                        ) : (
                            <p className="text-amber-800">No sealed record was returned for this bundle.</p>
                        )}
                        <div className="flex items-center gap-2 pt-1">
                            <button type="button" className={BUTTON_SECONDARY} onClick={download} disabled={!result.bundle} data-testid="bundle-download">
                                <Download className="w-3.5 h-3.5" /> Download JSON
                            </button>
                            <a href={OFFLINE_VERIFIER_URL} target="_blank" rel="noopener noreferrer" className="text-primary-600 hover:underline">
                                Verify offline →
                            </a>
                        </div>
                    </div>
                )}
            </div>
        </Section>
    );
}

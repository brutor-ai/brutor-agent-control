/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * External evidence attachments for one AI System.
 *
 * A row is a **pointer with a fingerprint** — the document itself stays in
 * the organisation's document system; the platform records where it lives,
 * its SHA-256, who produced it and whether that party was independent.
 *
 * Two things this panel is deliberate about:
 *
 * **Provenance is visible per row.** `independent` renders as one of three
 * states — independent, self-assessed, unstated — because "we don't know"
 * is a different claim from "we assessed ourselves", and the assurance
 * report treats them differently.
 *
 * **Expiry is a statement.** An expired artifact stays listed with an
 * explicit badge rather than disappearing: the lifecycle gate refuses on
 * it, and the operator needs to see *which* document needs renewing.
 *
 * **Supplier information is about one dependency** (EU AI
 * Act Art 25(4)): the written agreement with whoever supplies an external
 * agent or a shared service owned by another legal entity. It names the
 * dependency edge it covers and is pinned by hash (both required — the
 * control plane 422s otherwise). A row whose edge was removed stays listed
 * as detached: the agreement existed, it just backs no declared dependency.
 */

import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, Link2, Plus, Trash2 } from 'lucide-react';

import { HelpTip } from '../ui/HelpTip';

import { useGateway } from '../../hooks/useGateway';
import {
    AISystemEvidenceItem,
    DependencyEdge,
    EVIDENCE_KIND_LABELS,
    EvidenceCreateRequest,
    EvidenceKind,
    EvidenceListResponse,
} from '../../lib/aiSystemTypes';
import {
    dependencyEdgeLabel,
    needsSupplierInformation,
    supplierEdgesFirst,
} from '../../lib/composition';
import { errorKind } from '../../lib/narrow';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

/** The one evidence kind that names a dependency edge. */
const SUPPLIER_KIND: EvidenceKind = 'supplier_information';

interface EvidencePanelProps {
    aiSystemId: string;
    /** Re-query the lifecycle gate after evidence changes — attaching an
     *  impact assessment can flip a requirement from unmet to satisfied. */
    onChanged?: () => void;
}

const EMPTY_FORM: EvidenceCreateRequest = {
    kind: 'impact_assessment',
    title: '',
};

export function EvidencePanel({ aiSystemId, onChanged }: EvidencePanelProps) {
    const {
        fetchAISystemEvidence,
        createAISystemEvidence,
        deleteAISystemEvidence,
        gatewayClient,
    } = useGateway();

    const [listing, setListing] = useState<EvidenceListResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [adding, setAdding] = useState(false);
    const [busy, setBusy] = useState(false);
    const [form, setForm] = useState<EvidenceCreateRequest>(EMPTY_FORM);
    const [independent, setIndependent] = useState<'unstated' | 'yes' | 'no'>(
        'unstated'
    );

    const load = useCallback(async () => {
        try {
            setLoading(true);
            setError('');
            setListing(await fetchAISystemEvidence(aiSystemId));
        } catch (err: unknown) {
            console.error('Failed to load evidence:', errorKind(err));
            setError(err instanceof Error ? err.message : 'Failed to load evidence');
            setListing(null);
        } finally {
            setLoading(false);
        }
    }, [aiSystemId, fetchAISystemEvidence]);

    useEffect(() => {
        void load();
    }, [load]);

    // This system's declared dependency edges — what a supplier_information
    // pointer can cover. Best-effort: null = could not be loaded.
    const [edges, setEdges] = useState<DependencyEdge[] | null>(null);
    const [readFailure, reads] = useReadFailures<'dependencies'>();
    const [edgesAsked, setEdgesAsked] = useState(0);
    useEffect(() => {
        let cancelled = false;
        Promise.resolve()
            .then(() => gatewayClient.fetchAISystemDependencies(aiSystemId))
            .then(
                (res) => {
                    if (cancelled) return;
                    setEdges(res.dependencies);
                    reads.loaded('dependencies');
                },
                (err: unknown) => {
                    if (cancelled) return;
                    setEdges(null);
                    reads.lost('dependencies', 'Failed to load the dependencies of this system; missing supplier information is not shown', err);
                }
            );
        return () => {
            cancelled = true;
        };
    }, [aiSystemId, gatewayClient, edgesAsked, reads]);
    const edgeById = new Map((edges ?? []).map((e) => [e.id, e]));
    const pickerEdges = supplierEdgesFirst(edges ?? []);
    // Edges that need supplier information and have no unexpired pointer.
    const missingSupplier = (edges ?? []).filter(
        (e) =>
            needsSupplierInformation(e) &&
            !(listing?.evidence ?? []).some(
                (ev) => ev.kind === SUPPLIER_KIND && ev.dependency_id === e.id && !ev.is_expired
            )
    );

    const isSupplier = form.kind === SUPPLIER_KIND;
    const canAttach =
        Boolean(form.title.trim()) &&
        (!isSupplier || (Boolean(form.dependency_id) && Boolean(form.sha256?.trim())));

    const set = (patch: Partial<EvidenceCreateRequest>) =>
        setForm((f) => ({ ...f, ...patch }));

    const attach = async () => {
        try {
            setBusy(true);
            setError('');
            const payload: EvidenceCreateRequest = {
                ...form,
                title: form.title.trim(),
                uri: form.uri?.trim() || undefined,
                sha256: form.sha256?.trim() || undefined,
                assessor: form.assessor?.trim() || undefined,
                produced_at: form.produced_at || undefined,
                valid_until: form.valid_until || undefined,
                // Only supplier information names an edge (422 otherwise).
                dependency_id: isSupplier ? form.dependency_id || undefined : undefined,
                ...(independent === 'unstated'
                    ? {}
                    : { independent: independent === 'yes' }),
            };
            await createAISystemEvidence(aiSystemId, payload);
            setForm(EMPTY_FORM);
            setIndependent('unstated');
            setAdding(false);
            await load();
            onChanged?.();
        } catch (err: unknown) {
            setError(err instanceof Error ? err.message : 'Failed to attach evidence');
        } finally {
            setBusy(false);
        }
    };

    const detach = async (evidenceId: string) => {
        try {
            setBusy(true);
            setError('');
            await deleteAISystemEvidence(aiSystemId, evidenceId);
            await load();
            onChanged?.();
        } catch (err: unknown) {
            setError(err instanceof Error ? err.message : 'Failed to detach evidence');
        } finally {
            setBusy(false);
        }
    };

    return (
        <div className="space-y-3" data-testid="evidence-panel">
            <ReadFailureNotice failure={readFailure('dependencies')} onRetry={() => setEdgesAsked((n) => n + 1)} />
            <div className="flex items-start justify-between gap-4">
                <div className="flex items-start gap-2">
                    <h3 className="text-sm font-semibold text-default">Evidence</h3>
                    <HelpTip text="Externally produced assurance artifacts — impact assessments, evaluation, red-team and pen-test reports, and supplier information (the written agreement with whoever supplies a dependency, EU AI Act Art 25(4)). The document stays in your document system; the platform records where it lives and how to verify it. High-risk systems need an unexpired impact assessment to pass the approval gate." />
                </div>
                <button
                    type="button"
                    onClick={() => setAdding((a) => !a)}
                    className="flex items-center gap-2 px-3 py-1.5 text-sm bg-primary-600 hover:bg-primary-700 text-white rounded-lg"
                    data-testid="evidence-add-toggle"
                >
                    <Plus className="w-4 h-4" />
                    Attach evidence
                </button>
            </div>

            {error && (
                <div className="flex items-start gap-2 p-3 border border-red-200 bg-red-50 rounded">
                    <AlertTriangle className="w-4 h-4 text-red-600 shrink-0 mt-0.5" />
                    <span className="text-sm text-red-800">{error}</span>
                </div>
            )}

            {missingSupplier.length > 0 && (
                <div
                    className="p-3 border border-amber-300 bg-amber-50 rounded text-xs text-amber-900"
                    data-testid="evidence-supplier-missing"
                >
                    Supplier information missing for{' '}
                    {missingSupplier.map(dependencyEdgeLabel).join('; ')}. Attach the written
                    agreement for each (kind: Supplier information) — the EU AI Act Art 25(4)
                    obligation reads it.
                </div>
            )}

            {adding && (
                <div
                    className="bg-surface border border-default rounded-lg p-4 grid grid-cols-1 sm:grid-cols-2 gap-3"
                    data-testid="evidence-form"
                >
                    <label className="text-xs text-muted flex flex-col gap-1">
                        Kind
                        <select
                            value={form.kind}
                            onChange={(e) => set({ kind: e.target.value as EvidenceKind })}
                            className="px-2 py-1.5 text-sm border border-strong rounded text-default bg-surface"
                            data-testid="evidence-kind"
                        >
                            {Object.entries(EVIDENCE_KIND_LABELS).map(([k, label]) => (
                                <option key={k} value={k}>
                                    {label}
                                </option>
                            ))}
                        </select>
                    </label>
                    <label className="text-xs text-muted flex flex-col gap-1">
                        Title
                        <input
                            type="text"
                            value={form.title}
                            onChange={(e) => set({ title: e.target.value })}
                            placeholder="e.g. FRIA 2026 — Support Copilot"
                            className="px-2 py-1.5 text-sm border border-strong rounded"
                            data-testid="evidence-title"
                        />
                    </label>
                    <label className="text-xs text-muted flex flex-col gap-1 sm:col-span-2">
                        Document URL
                        <input
                            type="text"
                            value={form.uri ?? ''}
                            onChange={(e) => set({ uri: e.target.value })}
                            placeholder="https:// — where the document lives in your document system"
                            className="px-2 py-1.5 text-sm border border-strong rounded"
                            data-testid="evidence-uri"
                        />
                    </label>
                    {isSupplier && (
                        <label className="text-xs text-muted flex flex-col gap-1 sm:col-span-2">
                            <span>
                                Dependency it covers <span className="text-red-600">*</span>
                            </span>
                            <select
                                value={form.dependency_id ?? ''}
                                onChange={(e) => set({ dependency_id: e.target.value || undefined })}
                                className="px-2 py-1.5 text-sm border border-strong rounded text-default bg-surface"
                                data-testid="evidence-dependency"
                            >
                                <option value="">
                                    {edges === null
                                        ? 'Dependencies could not be loaded'
                                        : pickerEdges.length === 0
                                          ? 'This system declares no dependencies'
                                          : 'Select a dependency…'}
                                </option>
                                {pickerEdges.map((e) => (
                                    <option key={e.id} value={e.id}>
                                        {dependencyEdgeLabel(e)}
                                        {needsSupplierInformation(e) ? '' : ' (no supplier entity)'}
                                    </option>
                                ))}
                            </select>
                            <span>
                                The agreement with the supplier of this dependency. External agents and
                                services of another legal entity need one.
                            </span>
                        </label>
                    )}
                    <label className="text-xs text-muted flex flex-col gap-1 sm:col-span-2">
                        {isSupplier ? (
                            <span>
                                SHA-256 of the agreement <span className="text-red-600">*</span> — supplier
                                information is pinned by hash
                            </span>
                        ) : (
                            'SHA-256 of the document (optional — lets a reviewer verify the file)'
                        )}
                        <input
                            type="text"
                            value={form.sha256 ?? ''}
                            onChange={(e) => set({ sha256: e.target.value })}
                            placeholder="64 hex characters"
                            className="px-2 py-1.5 text-sm border border-strong rounded font-mono"
                            data-testid="evidence-sha256"
                        />
                    </label>
                    <label className="text-xs text-muted flex flex-col gap-1">
                        Assessor
                        <input
                            type="text"
                            value={form.assessor ?? ''}
                            onChange={(e) => set({ assessor: e.target.value })}
                            placeholder="Person, team or firm"
                            className="px-2 py-1.5 text-sm border border-strong rounded"
                            data-testid="evidence-assessor"
                        />
                    </label>
                    <label className="text-xs text-muted flex flex-col gap-1">
                        Independent of the building team?
                        <select
                            value={independent}
                            onChange={(e) =>
                                setIndependent(e.target.value as 'unstated' | 'yes' | 'no')
                            }
                            className="px-2 py-1.5 text-sm border border-strong rounded text-default bg-surface"
                            data-testid="evidence-independent"
                        >
                            <option value="unstated">Unstated</option>
                            <option value="yes">Yes — independently produced</option>
                            <option value="no">No — self-assessed</option>
                        </select>
                    </label>
                    <label className="text-xs text-muted flex flex-col gap-1">
                        Produced on
                        <input
                            type="date"
                            value={form.produced_at ?? ''}
                            onChange={(e) => set({ produced_at: e.target.value })}
                            className="px-2 py-1.5 text-sm border border-strong rounded"
                            data-testid="evidence-produced"
                        />
                    </label>
                    <label className="text-xs text-muted flex flex-col gap-1">
                        Valid until (optional)
                        <input
                            type="date"
                            value={form.valid_until ?? ''}
                            onChange={(e) => set({ valid_until: e.target.value })}
                            className="px-2 py-1.5 text-sm border border-strong rounded"
                            data-testid="evidence-valid-until"
                        />
                    </label>
                    <div className="sm:col-span-2 flex justify-end gap-2">
                        <button
                            type="button"
                            onClick={() => setAdding(false)}
                            className="px-3 py-1.5 text-xs border border-strong rounded-lg hover:bg-surface-hover"
                        >
                            Cancel
                        </button>
                        <button
                            type="button"
                            disabled={busy || !canAttach}
                            onClick={() => void attach()}
                            className="px-3 py-1.5 text-xs border border-strong rounded-lg hover:bg-surface-hover disabled:opacity-50"
                            data-testid="evidence-save"
                        >
                            Attach
                        </button>
                    </div>
                </div>
            )}

            {!loading && listing && listing.total === 0 && !adding && (
                <div
                    className="p-4 text-center border border-dashed border-strong rounded-lg"
                    data-testid="evidence-empty"
                >
                    <p className="text-sm text-muted">
                        No external evidence attached. Everything the assurance report
                        shows for this system is platform-generated — and the report
                        says so.
                    </p>
                </div>
            )}

            {listing?.evidence.map((e) => (
                <EvidenceRow
                    key={e.id}
                    item={e}
                    busy={busy}
                    edge={e.dependency_id ? edgeById.get(e.dependency_id) ?? null : null}
                    onDetach={() => void detach(e.id)}
                />
            ))}
        </div>
    );
}

function EvidenceRow({
    item,
    busy,
    edge,
    onDetach,
}: {
    item: AISystemEvidenceItem;
    busy: boolean;
    /** The covered edge, when the row names one and it is loaded. */
    edge: DependencyEdge | null;
    onDetach: () => void;
}) {
    const provenance =
        item.independent === true
            ? { label: 'independent', cls: 'bg-green-100 text-green-800' }
            : item.independent === false
              ? { label: 'self-assessed', cls: 'bg-slate-100 text-slate-700' }
              : { label: 'provenance unstated', cls: 'bg-slate-100 text-slate-500' };

    return (
        <div
            className="bg-surface border border-default rounded-lg p-3 flex items-start justify-between gap-3"
            data-testid={`evidence-${item.id}`}
        >
            <div className="min-w-0">
                <p className="text-sm font-medium text-default flex items-center gap-2 flex-wrap">
                    {item.title}
                    <span className="inline-flex px-2 py-0.5 rounded text-xs font-medium bg-sky-100 text-sky-800">
                        {EVIDENCE_KIND_LABELS[item.kind] ?? item.kind}
                    </span>
                    <span
                        className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${provenance.cls}`}
                        data-testid="evidence-provenance"
                    >
                        {provenance.label}
                    </span>
                    {item.is_expired && (
                        <span
                            className="inline-flex px-2 py-0.5 rounded text-xs font-medium bg-amber-100 text-amber-800"
                            data-testid="evidence-expired"
                        >
                            expired
                        </span>
                    )}
                </p>
                {item.kind === SUPPLIER_KIND && (
                    <p
                        className={`text-xs mt-1 ${item.dependency_id ? 'text-secondary' : 'text-amber-800'}`}
                        data-testid={`evidence-covers-${item.id}`}
                        data-detached={item.dependency_id ? 'false' : 'true'}
                    >
                        {item.dependency_id
                            ? `Covers: ${edge ? dependencyEdgeLabel(edge) : `dependency ${item.dependency_id}`}`
                            : 'Detached — the dependency it covered was removed; it backs no declared dependency'}
                    </p>
                )}
                <p className="text-xs text-muted mt-1">
                    {item.assessor ? `By ${item.assessor}` : 'Assessor not recorded'}
                    {item.produced_at
                        ? ` · produced ${new Date(item.produced_at).toLocaleDateString()}`
                        : ''}
                    {item.valid_until
                        ? ` · valid until ${new Date(item.valid_until).toLocaleDateString()}`
                        : ''}
                </p>
                {item.uri && (
                    <a
                        href={item.uri}
                        target="_blank"
                        rel="noreferrer"
                        className="text-xs text-sky-700 hover:underline inline-flex items-center gap-1 mt-1"
                    >
                        <Link2 className="w-3 h-3" />
                        {item.uri}
                    </a>
                )}
                {item.sha256 && (
                    <p className="text-[11px] font-mono text-muted mt-1 truncate">
                        sha256:{item.sha256}
                    </p>
                )}
            </div>
            <button
                type="button"
                disabled={busy}
                onClick={onDetach}
                title="Detach this evidence"
                className="shrink-0 p-1.5 text-muted hover:text-red-600 disabled:opacity-50"
                data-testid={`evidence-delete-${item.id}`}
            >
                <Trash2 className="w-4 h-4" />
            </button>
        </div>
    );
}

export default EvidencePanel;

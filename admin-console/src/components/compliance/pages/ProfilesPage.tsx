/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Compliance → Profiles.
 *
 * The profile table (`GET /compliance/systems`): every AI System with
 * whether a profile is declared, whether that declaration is sealed, the
 * frameworks active for it, its retention floor, and — for the selected
 * framework — the declared role and classification. Selecting a row opens
 * the profile editor (neutral core + the framework's facts, validated
 * against the catalog's vocabularies) and the documentation export for
 * that system: a versioned skeleton rendered from contract, register,
 * profile, bindings and evidence pointers — a skeleton, not a claim.
 */

import { useCallback, useState } from 'react';
import { ExternalLink, FileText, Loader2 } from 'lucide-react';

import { useGateway } from '../../../hooks/useGateway';
import { useKeyedLoad } from '../../../hooks/useKeyedLoad';
import { useToast } from '../../common/Toast';
import { errorMessage, getRecord, getString } from '../../../lib/narrow';
import {
    classificationFieldFor,
    roleFieldFor,
    shortHash,
    type ComplianceSystemRow,
    type DocumentationExport,
} from '../../../lib/complianceEvidenceTypes';
import { TABLE_CONTAINER, TABLE_ROW_HOVER, TABLE_TBODY, TABLE_TH, TABLE_THEAD } from '../../ui/tableStyles';
import ProfileEditor from '../ProfileEditor';
import RecordDialog from '../RecordDialog';
import { ErrorNote, Loading, RecordLink, SealNote, Section } from '../ComplianceUi';
import { BUTTON_SECONDARY, formatWhen, LINK_BUTTON } from '../complianceFormat';
import type { CompliancePageProps } from '../complianceSections';

export default function ProfilesPage({ framework, frameworks, setActiveTab }: CompliancePageProps) {
    const { gatewayClient } = useGateway();
    const [selectedId, setSelectedId] = useState<string | null>(null);
    const [openRecord, setOpenRecord] = useState<string | null>(null);

    const loader = useCallback(() => gatewayClient.listComplianceSystems(), [gatewayClient]);
    const { data, error, loading, reload } = useKeyedLoad('compliance-profiles', loader, 'Failed to load AI Systems');

    if (loading && !data) return <Loading label="Loading AI Systems…" testId="systems-loading" />;
    if (error) return <ErrorNote message={error} testId="systems-error" />;

    const rows = data?.items ?? [];
    const selected = rows.find((s) => s.ai_system_id === selectedId) ?? null;
    const registryEntry = frameworks.find((f) => f.id === framework);
    const roleField = roleFieldFor(framework);
    const classificationField = classificationFieldFor(framework);

    return (
        <div className="space-y-4" data-testid="compliance-profiles">
            <div className="grid grid-cols-1 xl:grid-cols-5 gap-4">
                <div className="xl:col-span-3 min-w-0">
                    <div className={`${TABLE_CONTAINER} overflow-x-auto`}>
                        <table className="w-full text-sm">
                            <thead className={TABLE_THEAD}>
                                <tr>
                                    <th className={TABLE_TH}>AI System</th>
                                    <th className={TABLE_TH}>Profile</th>
                                    <th className={TABLE_TH}>{registryEntry?.title ?? framework}</th>
                                    <th className={TABLE_TH}>Frameworks</th>
                                    <th className={TABLE_TH}>Retention floor</th>
                                </tr>
                            </thead>
                            <tbody className={TABLE_TBODY}>
                                {rows.length === 0 && (
                                    <tr>
                                        <td colSpan={5} className="px-4 py-6 text-center text-muted text-xs" data-testid="systems-empty">
                                            No AI Systems yet — create one under AI Estate → AI Systems.
                                        </td>
                                    </tr>
                                )}
                                {rows.map((s) => (
                                    <SystemRow
                                        key={s.ai_system_id}
                                        row={s}
                                        framework={framework}
                                        roleField={roleField}
                                        classificationField={classificationField}
                                        selected={selectedId === s.ai_system_id}
                                        onSelect={() => setSelectedId(s.ai_system_id)}
                                        onOpenRecord={setOpenRecord}
                                    />
                                ))}
                            </tbody>
                        </table>
                        <div className="flex items-center justify-between border-t border-default px-4 py-2 text-xs text-muted gap-2 flex-wrap">
                            <span>
                                {rows.length} AI System{rows.length === 1 ? '' : 's'} · enabled frameworks:{' '}
                                {(data?.frameworks_enabled ?? []).join(', ') || 'none'}
                            </span>
                            {selected && setActiveTab && (
                                <button
                                    type="button"
                                    className={LINK_BUTTON}
                                    onClick={() => setActiveTab(`ai-systems:${selected.ai_system_id}:compliance`)}
                                    data-testid="systems-open-detail"
                                >
                                    Open {selected.name} →
                                </button>
                            )}
                        </div>
                    </div>
                </div>

                <div className="xl:col-span-2 min-w-0">
                    <div className="rounded-lg border border-default bg-surface p-4">
                        {selected ? (
                            <ProfileEditor
                                key={`${selected.ai_system_id}:${framework}`}
                                aiSystemId={selected.ai_system_id}
                                framework={framework}
                                frameworks={frameworks}
                                onSaved={reload}
                                onOpenRecord={setOpenRecord}
                            />
                        ) : (
                            <p className="text-xs text-muted py-8 text-center" data-testid="systems-no-selection">
                                Select an AI System to declare its compliance profile.
                            </p>
                        )}
                    </div>
                </div>
            </div>

            {selected && (
                <DocumentationPanel
                    key={`${selected.ai_system_id}:${framework}`}
                    aiSystemId={selected.ai_system_id}
                    systemName={selected.name}
                    framework={framework}
                    frameworkTitle={registryEntry?.title ?? framework}
                    onOpenRecord={setOpenRecord}
                />
            )}

            {openRecord && <RecordDialog recordId={openRecord} onClose={() => setOpenRecord(null)} />}
        </div>
    );
}

function SystemRow({
    row,
    framework,
    roleField,
    classificationField,
    selected,
    onSelect,
    onOpenRecord,
}: {
    row: ComplianceSystemRow;
    framework: string;
    roleField: string;
    classificationField: string;
    selected: boolean;
    onSelect: () => void;
    onOpenRecord: (id: string) => void;
}) {
    const block = getRecord(row.frameworks, framework);
    const role = getString(block, roleField);
    const classification = getString(block, classificationField);
    return (
        <tr
            className={`${TABLE_ROW_HOVER} cursor-pointer ${selected ? 'bg-surface-alt' : ''}`}
            onClick={onSelect}
            data-testid={`system-row-${row.ai_system_id}`}
            aria-selected={selected}
        >
            <td className="px-4 py-2.5">
                <div className="font-medium text-default">{row.name}</div>
                <div className="text-xs text-muted">
                    <span className="font-mono">{row.ai_system_id}</span>
                    {row.lifecycle_stage ? ` · ${row.lifecycle_stage}` : ''}
                </div>
            </td>
            <td className="px-4 py-2.5 text-xs" data-testid={`system-profile-${row.ai_system_id}`}>
                {!row.exists ? (
                    <span className="text-amber-800">not declared</span>
                ) : row.sealed && row.declaration_record_id ? (
                    <span className="text-secondary" onClick={(e) => e.stopPropagation()}>
                        sealed · <RecordLink recordId={row.declaration_record_id} onOpen={onOpenRecord} />
                    </span>
                ) : (
                    <span className="text-amber-800">declared · not sealed</span>
                )}
                {row.updated_at && <div className="text-muted">{formatWhen(row.updated_at)}</div>}
            </td>
            <td className="px-4 py-2.5 text-xs" data-testid={`system-fw-${row.ai_system_id}`}>
                {role || classification ? (
                    <span className="text-secondary">
                        {role ?? 'role not declared'} · {classification ?? 'classification not declared'}
                    </span>
                ) : (
                    <span className="text-amber-800">not declared</span>
                )}
            </td>
            <td className="px-4 py-2.5 text-xs text-secondary">{(row.active_frameworks ?? []).join(', ') || '—'}</td>
            <td className="px-4 py-2.5 text-xs text-secondary">
                {row.retention_floor_days != null ? `${row.retention_floor_days} days` : '—'}
            </td>
        </tr>
    );
}

function DocumentationPanel({
    aiSystemId,
    systemName,
    framework,
    frameworkTitle,
    onOpenRecord,
}: {
    aiSystemId: string;
    systemName: string;
    framework: string;
    frameworkTitle: string;
    onOpenRecord: (id: string) => void;
}) {
    const { gatewayClient } = useGateway();
    const toast = useToast();
    const [busy, setBusy] = useState<'json' | 'html' | null>(null);
    const [latest, setLatest] = useState<DocumentationExport | null>(null);

    const loader = useCallback(
        () => gatewayClient.listSystemDocumentationExports(aiSystemId, framework),
        [gatewayClient, aiSystemId, framework],
    );
    const { data, error, reload } = useKeyedLoad(`${aiSystemId}:${framework}`, loader, 'Failed to load exports');

    const exportJson = async () => {
        setBusy('json');
        try {
            const doc = await gatewayClient.exportSystemDocumentation(aiSystemId, framework);
            setLatest(doc);
            reload();
            toast.success(doc.reused ? 'Unchanged — the previous export stands' : 'Documentation skeleton exported');
        } catch (e: unknown) {
            toast.error(`Export failed: ${errorMessage(e)}`);
        } finally {
            setBusy(null);
        }
    };

    const openHtml = async () => {
        setBusy('html');
        // Open the tab synchronously (popup blockers), then fill it.
        const tab = window.open('', '_blank');
        try {
            const html = await gatewayClient.exportSystemDocumentationHtml(aiSystemId, framework);
            const url = URL.createObjectURL(new Blob([html], { type: 'text/html' }));
            if (tab) tab.location.href = url;
            else window.open(url, '_blank', 'noopener');
            setTimeout(() => URL.revokeObjectURL(url), 60_000);
            reload();
        } catch (e: unknown) {
            tab?.close();
            toast.error(`Export failed: ${errorMessage(e)}`);
        } finally {
            setBusy(null);
        }
    };

    const items = data?.items ?? [];
    return (
        <Section
            title={`Documentation — ${systemName}`}
            subtitle={`${frameworkTitle} template, rendered from the contract, register, profile, bindings and evidence pointers. A skeleton, not a claim: it records what the platform holds, and the digest of each export is sealed.`}
            actions={
                <>
                    <button type="button" className={BUTTON_SECONDARY} onClick={() => void exportJson()} disabled={busy !== null} data-testid="doc-export-json">
                        {busy === 'json' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <FileText className="w-3.5 h-3.5" />}
                        Export
                    </button>
                    <button type="button" className={BUTTON_SECONDARY} onClick={() => void openHtml()} disabled={busy !== null} data-testid="doc-export-html">
                        {busy === 'html' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <ExternalLink className="w-3.5 h-3.5" />}
                        Open HTML
                    </button>
                </>
            }
            testId="documentation-panel"
        >
            {latest && (
                <div className="mb-3 space-y-1 text-xs" data-testid="doc-latest">
                    <p className="text-secondary">
                        {latest.skeleton ? 'Skeleton' : 'Document'} · template {latest.template}
                        {latest.contract_version != null ? ` · contract v${latest.contract_version}` : ''} · digest{' '}
                        <code className="font-mono" title={latest.digest}>
                            {shortHash(latest.digest, 16)}
                        </code>
                        {latest.reused ? ' · unchanged since the previous export' : ''}
                    </p>
                    <SealNote seal={latest.seal} what="export" onOpenRecord={onOpenRecord} testId="doc-seal" />
                </div>
            )}
            <ErrorNote message={error} testId="doc-error" />
            {items.length === 0 ? (
                <p className="text-xs text-muted" data-testid="doc-empty">
                    No export yet for this framework.
                </p>
            ) : (
                <ul className="divide-y divide-default text-xs" data-testid="doc-exports">
                    {items.map((x) => (
                        <li key={x.id} className="py-1.5 flex items-center gap-3 flex-wrap" data-testid={`doc-export-${x.id}`}>
                            <span className="text-secondary whitespace-nowrap">{formatWhen(x.created_at)}</span>
                            <span className="text-default">{x.template}</span>
                            {x.contract_version != null && <span className="text-muted">contract v{x.contract_version}</span>}
                            <code className="font-mono text-secondary" title={x.digest}>
                                {shortHash(x.digest, 16)}
                            </code>
                            {x.record_id ? (
                                <RecordLink recordId={x.record_id} onOpen={onOpenRecord} />
                            ) : (
                                <span className="text-amber-800">not sealed</span>
                            )}
                            {x.created_by && <span className="text-muted ml-auto">{x.created_by}</span>}
                        </li>
                    ))}
                </ul>
            )}
        </Section>
    );
}

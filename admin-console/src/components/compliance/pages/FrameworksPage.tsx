/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Compliance → Frameworks.
 *
 * The registry is data: each row is a framework the platform knows, with
 * the catalog version its obligations were written against, whether that
 * catalog was checked against the published instrument, its retention
 * floor and its incident deadline rules. A catalog that was not checked
 * carries the banner "verify against the published instrument before
 * relying on this" — the obligations still render, but the operator is told
 * what they rest on. Below the table, the selected framework's catalog
 * (dated versions, role and classification vocabularies, floors by
 * classification) and its request-log tagging. Enabling a framework is the
 * same tenant switch the tagging engine reads, so the table carries
 * the tagged-row count and the selected framework's panel carries the
 * per-tag breakdown, the tagging guide and the auditor CSV export.
 */

import { useCallback, useMemo, useState } from 'react';
import { AlertTriangle } from 'lucide-react';

import { useGateway } from '../../../hooks/useGateway';
import { useKeyedLoad } from '../../../hooks/useKeyedLoad';
import { useToast } from '../../common/Toast';
import { errorMessage, getString } from '../../../lib/narrow';
import { TABLE_CONTAINER, TABLE_TBODY, TABLE_TH, TABLE_THEAD } from '../../ui/tableStyles';
import FrameworkTaggingPanel from '../FrameworkTaggingPanel';
import { cardsByTagKey } from '../frameworkTagging';
import { CompositeClassificationNote, ErrorNote, Loading, Section } from '../ComplianceUi';
import { deadlineRulesText, NOT_LEGAL_ADVICE, windowFor, type WindowPreset } from '../complianceFormat';
import { useFrameworkCatalog } from '../useFrameworkCatalog';
import type { CompliancePageProps } from '../complianceSections';

const KIND_CHIP: Record<string, string> = {
    regulation: 'bg-blue-100 text-blue-800',
    standard: 'bg-surface-raised text-secondary',
    attestation: 'bg-purple-100 text-purple-800',
    voluntary: 'bg-surface-raised text-secondary',
    framework: 'bg-surface-raised text-secondary',
};


export default function FrameworksPage({ framework, registry, refreshFrameworks }: CompliancePageProps) {
    // The whole registry: frameworks not offered are listed with the reason.
    const frameworks = registry;
    const { gatewayClient } = useGateway();
    const toast = useToast();
    const [busy, setBusy] = useState<string | null>(null);

    const selected = frameworks.find((f) => f.id === framework);

    // Tagged request-log rows per framework, for one window shared by the
    // table column, the selected framework's breakdown and its CSV export.
    const [preset, setPreset] = useState<WindowPreset>('30d');
    const taggingWindow = useMemo(() => windowFor(preset), [preset]);
    const loadTagging = useCallback(
        () => gatewayClient.getComplianceDashboard(taggingWindow),
        [gatewayClient, taggingWindow],
    );
    const tagging = useKeyedLoad(`tagging:${preset}`, loadTagging, 'Failed to load tagged-row counts');
    const cards = useMemo(() => cardsByTagKey(tagging.data?.cards), [tagging.data]);
    const windowLabel = preset.replace('d', ' days');

    const toggle = async (id: string, enabled: boolean) => {
        setBusy(id);
        try {
            await gatewayClient.setComplianceFrameworkEnabled(id, enabled);
            refreshFrameworks();
            toast.success(`${enabled ? 'Enabled' : 'Disabled'} ${id} for this tenant`);
        } catch (e: unknown) {
            toast.error(`Update failed: ${errorMessage(e)}`);
        } finally {
            setBusy(null);
        }
    };

    return (
        <div className="space-y-6" data-testid="compliance-frameworks">
            <div className={`${TABLE_CONTAINER} overflow-x-auto`}>
                <table className="w-full text-sm">
                    <thead className={TABLE_THEAD}>
                        <tr>
                            <th className={TABLE_TH}>Framework</th>
                            <th className={TABLE_TH}>Kind</th>
                            <th className={TABLE_TH}>Jurisdiction</th>
                            <th className={TABLE_TH}>Catalog</th>
                            <th className={TABLE_TH}>Retention floor</th>
                            <th className={TABLE_TH}>Deadline rules</th>
                            <th className={`${TABLE_TH} text-right`}>Obligations</th>
                            <th className={`${TABLE_TH} text-right`} title={`Request-log rows tagged for the framework, last ${windowLabel}`}>
                                Tagged rows
                            </th>
                            <th className={TABLE_TH}>Enabled</th>
                        </tr>
                    </thead>
                    <tbody className={TABLE_TBODY}>
                        {frameworks.length === 0 && (
                            <tr>
                                <td colSpan={9} className="px-4 py-6 text-center text-muted text-xs" data-testid="frameworks-empty">
                                    The registry is empty — nothing has been seeded for this deployment.
                                </td>
                            </tr>
                        )}
                        {frameworks.map((f) => (
                            <tr key={f.id} data-testid={`framework-row-${f.id}`} className={f.id === framework ? 'bg-surface-alt' : ''}>
                                <td className="px-4 py-3">
                                    <div className="font-medium text-default">{f.title}</div>
                                    <div className="text-xs text-muted">{f.reference}</div>
                                    {!f.offered && (
                                        <div
                                            className="mt-1 inline-flex items-start gap-1 text-xs text-amber-800 bg-amber-100 rounded px-1.5 py-0.5"
                                            data-testid={`framework-unavailable-${f.id}`}
                                        >
                                            <AlertTriangle className="w-3 h-3 mt-0.5 flex-none" />
                                            {f.unavailable_reason ?? 'Not available.'}
                                        </div>
                                    )}
                                </td>
                                <td className="px-4 py-3">
                                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase ${KIND_CHIP[f.kind] ?? KIND_CHIP.framework}`}>
                                        {f.kind}
                                    </span>
                                </td>
                                <td className="px-4 py-3 text-secondary">{f.jurisdiction}</td>
                                <td className="px-4 py-3 font-mono text-xs text-secondary">{f.catalog_version}</td>
                                <td className="px-4 py-3 text-secondary">
                                    {f.retention_floor_days != null ? `${f.retention_floor_days} days` : '—'}
                                </td>
                                <td className="px-4 py-3 text-xs text-secondary" data-testid={`framework-deadlines-${f.id}`}>
                                    {deadlineRulesText(f.deadline_rules) || <span className="text-muted">none</span>}
                                </td>
                                <td className="px-4 py-3 text-right text-default">
                                    {f.obligation_count > 0 ? f.obligation_count : <span className="text-muted">none yet</span>}
                                </td>
                                <td className="px-4 py-3 text-right text-default" data-testid={`framework-tagged-${f.id}`}>
                                    {!f.tag_key ? (
                                        <span className="text-muted" title="This framework does not tag request-log rows">not tagged</span>
                                    ) : cards[f.tag_key] ? (
                                        cards[f.tag_key].total_tagged_rows.toLocaleString()
                                    ) : (
                                        <span className="text-muted">{tagging.error ? 'unavailable' : '…'}</span>
                                    )}
                                </td>
                                <td className="px-4 py-3">
                                    <label className="inline-flex items-center gap-2 cursor-pointer">
                                        <input
                                            type="checkbox"
                                            checked={f.enabled}
                                            // Not offered: it cannot be switched on;
                                            // one switched on earlier reads as off and can only stay off.
                                            disabled={busy === f.id || !f.offered}
                                            title={f.offered ? undefined : f.unavailable_reason ?? undefined}
                                            onChange={(e) => void toggle(f.id, e.target.checked)}
                                            className="h-4 w-4 rounded border-default"
                                            data-testid={`framework-toggle-${f.id}`}
                                            aria-label={`Enable ${f.title}`}
                                        />
                                        <span className="text-xs text-secondary">{f.enabled ? 'on' : 'off'}</span>
                                    </label>
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>

            {selected && (
                <CatalogPanel
                    key={selected.id}
                    frameworkId={selected.id}
                    title={selected.title}
                    hasCatalog={selected.obligation_count > 0}
                />
            )}

            {selected && (
                <FrameworkTaggingPanel
                    title={selected.title}
                    tagKey={selected.tag_key}
                    enabled={selected.enabled}
                    card={selected.tag_key ? cards[selected.tag_key] : undefined}
                    loading={tagging.loading}
                    error={tagging.error}
                    preset={preset}
                    onPreset={setPreset}
                    window={taggingWindow}
                />
            )}
        </div>
    );
}

function CatalogPanel({ frameworkId, title, hasCatalog }: { frameworkId: string; title: string; hasCatalog: boolean }) {
    const { data: catalog, error, loading } = useFrameworkCatalog(frameworkId, hasCatalog);
    return (
        <Section
            title={`${title} — catalog`}
            subtitle={`The selected framework's vocabularies and dated catalog versions. ${NOT_LEGAL_ADVICE}`}
            testId="framework-catalog"
        >
            {!hasCatalog ? (
                <p className="text-xs text-muted" data-testid="framework-catalog-none">
                    No obligation catalog for this framework — it is a registry entry only.
                </p>
            ) : loading && !catalog ? (
                <Loading />
            ) : error ? (
                <ErrorNote message={error} testId="framework-catalog-error" />
            ) : catalog ? (
                <dl className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-3 text-xs">
                    <div>
                        <dt className="text-muted uppercase tracking-wide">Versions</dt>
                        <dd className="mt-1 space-y-0.5" data-testid="framework-catalog-versions">
                            {(catalog.versions ?? []).length === 0 && <span className="text-muted">unversioned</span>}
                            {(catalog.versions ?? []).map((v, i) => (
                                <div key={getString(v, 'catalog_version') ?? i} className="font-mono text-secondary">
                                    {getString(v, 'catalog_version') ?? '—'} · from {getString(v, 'effective_from') ?? '—'}
                                    {getString(v, 'effective_to') ? ` until ${getString(v, 'effective_to')}` : ' (in force)'}
                                </div>
                            ))}
                        </dd>
                    </div>
                    <div>
                        <dt className="text-muted uppercase tracking-wide">Retention floors by classification</dt>
                        <dd className="mt-1 text-secondary" data-testid="framework-catalog-floors">
                            {Object.keys(catalog.retention_floors ?? {}).length === 0
                                ? 'none mandated'
                                : Object.entries(catalog.retention_floors ?? {})
                                      .map(([k, v]) => `${k === '*' ? 'any' : k}: ${String(v)} days`)
                                      .join(' · ')}
                        </dd>
                    </div>
                    <div>
                        <dt className="text-muted uppercase tracking-wide">Roles</dt>
                        <dd className="mt-1 text-secondary">{(catalog.roles ?? []).join(', ') || '—'}</dd>
                    </div>
                    <div>
                        <dt className="text-muted uppercase tracking-wide">Classifications</dt>
                        <dd className="mt-1 text-secondary">{(catalog.classifications ?? []).join(', ') || '—'}</dd>
                    </div>
                    {catalog.composite_classification && (
                        <div className="md:col-span-2">
                            <dt className="text-muted uppercase tracking-wide">Classification along the value chain</dt>
                            <dd className="mt-1">
                                <CompositeClassificationNote testId="framework-composite-classification" />
                            </dd>
                        </div>
                    )}
                    <div className="md:col-span-2">
                        <dt className="text-muted uppercase tracking-wide">Incident deadline rules</dt>
                        <dd className="mt-1 text-secondary">
                            {deadlineRulesText(catalog.deadline_rules) || 'none'} — shown on incidents, never enforced.
                        </dd>
                    </div>
                </dl>
            ) : null}
        </Section>
    );
}

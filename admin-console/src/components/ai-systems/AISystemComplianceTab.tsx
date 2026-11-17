/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * AI System detail → Compliance.
 *
 * The console's per-system panel (profile, obligations per framework with
 * their evidence statements, sealed-record count) sits on top; below it,
 * what an auditor asks for next about this one system:
 *
 *   - whether the profile declaration is sealed (and why not, if not);
 *   - the latest Evidence Report per framework, with its sealed record and
 *     countersignature status;
 *   - the documentation export for a framework — generated on request,
 *     never on page load, because each new export seals a record;
 *   - the AI-interaction notice the portal resolves for the system.
 */

import { useCallback, useState } from 'react';
import { Bell, Download, FileText, Loader2, ScrollText, ShieldCheck } from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { useKeyedLoad } from '../../hooks/useKeyedLoad';
import { failureSaying } from '../../hooks/useRead';
import { downloadJson } from '../../lib/downloadJson';
import { errorMessage, failureSentence } from '../../lib/narrow';
import { shortHash, type AISystemComplianceProfile, type ComplianceFramework } from '../../lib/complianceEvidenceTypes';
import type {
    ComplianceReportSummary,
    DocumentationExportSummary,
    EffectiveNoticeConfig,
} from '../../lib/evidenceSurfaceTypes';
import ReadFailureNotice from '../common/ReadFailureNotice';
import SystemCompliancePanel from '../compliance/SystemCompliancePanel';
import { DEFAULT_FRAMEWORK, readStoredFramework } from '../compliance/complianceSections';

interface Props {
    aiSystemId: string;
    setActiveTab?: (tab: string) => void;
}

interface EvidenceData {
    profile: AISystemComplianceProfile | null;
    frameworks: ComplianceFramework[];
    reports: ComplianceReportSummary[] | null;
    notice: EffectiveNoticeConfig | null;
    exports: DocumentationExportSummary[] | null;
    /** Why each read could not be answered; '' where it was. */
    failed: Record<'profile' | 'frameworks' | 'reports' | 'notice' | 'exports', string>;
}

/** Newest report per framework (the API sorts newest period first). */
export function latestReportPerFramework(reports: ComplianceReportSummary[]): ComplianceReportSummary[] {
    const seen = new Map<string, ComplianceReportSummary>();
    for (const r of reports) if (!seen.has(r.framework_id)) seen.set(r.framework_id, r);
    return [...seen.values()];
}

const COUNTERSIGNATURE_LABEL: Record<string, string> = {
    countersigned: 'countersigned',
    awaiting_countersignature: 'awaiting countersignature',
    none: 'no bundle',
};

const CARD = 'rounded-lg border border-default bg-surface p-3';
const CARD_TITLE = 'text-xs font-semibold text-muted uppercase tracking-wide mb-1 flex items-center gap-1.5';

export default function AISystemComplianceTab({ aiSystemId, setActiveTab }: Props) {
    const { gatewayClient } = useGateway();
    const [docFramework, setDocFramework] = useState<string>(() => readStoredFramework() ?? DEFAULT_FRAMEWORK);
    const [exporting, setExporting] = useState<'json' | 'html' | null>(null);
    const [exportNote, setExportNote] = useState<{ tone: 'ok' | 'warn' | 'error'; text: string } | null>(null);

    const loader = useCallback(async (): Promise<EvidenceData> => {
        const [p, f, r, n, x] = await Promise.allSettled([
            gatewayClient.getAISystemComplianceProfile(aiSystemId),
            gatewayClient.listComplianceFrameworks(),
            gatewayClient.listSystemComplianceReports(aiSystemId, { limit: 100 }),
            gatewayClient.getEffectiveNoticeConfig(aiSystemId),
            gatewayClient.listSystemDocumentationExports(aiSystemId),
        ]);
        // Five reads, each of its own card: one that failed says so there,
        // with what the Control Plane said, and the others are shown.
        return {
            profile: p.status === 'fulfilled' ? p.value : null,
            // Offered frameworks only (verified against their published text).
            frameworks: f.status === 'fulfilled' ? f.value.items.filter((x) => x.offered) : [],
            reports: r.status === 'fulfilled' ? r.value.items : null,
            notice: n.status === 'fulfilled' ? n.value : null,
            exports: x.status === 'fulfilled' ? x.value.items : null,
            failed: {
                profile: p.status === 'rejected' ? failureSentence('Failed to load the profile declaration', p.reason) : '',
                frameworks: f.status === 'rejected' ? failureSentence('Failed to load the frameworks to export under', f.reason) : '',
                reports: r.status === 'rejected' ? failureSentence('Failed to load the reports', r.reason) : '',
                notice: n.status === 'rejected' ? failureSentence('Failed to load the notice configuration', n.reason) : '',
                exports: x.status === 'rejected' ? failureSentence('Failed to load the earlier exports', x.reason) : '',
            },
        };
    }, [gatewayClient, aiSystemId]);
    const { data, reload } = useKeyedLoad(`system-compliance-evidence:${aiSystemId}`, loader);

    const frameworkTitle = (id: string) => data?.frameworks.find((f) => f.id === id)?.title ?? id;

    const exportJson = async () => {
        setExporting('json');
        setExportNote(null);
        try {
            const doc = await gatewayClient.exportSystemDocumentation(aiSystemId, docFramework);
            downloadJson(`documentation-${aiSystemId}-${docFramework}.json`, doc);
            const sealed = doc.seal.sealed && doc.record_id;
            setExportNote({
                tone: sealed ? 'ok' : 'warn',
                text: `${doc.reused ? 'Unchanged since the last export' : 'Exported'} · digest ${shortHash(doc.digest, 16)} · ${
                    sealed ? `sealed as ${shortHash(doc.record_id, 16)}` : `not sealed${doc.seal.seal_error ? ` (${doc.seal.seal_error})` : ''}`
                }. A skeleton generated from configuration — review before use.`,
            });
            reload();
        } catch (e: unknown) {
            setExportNote({ tone: 'error', text: `Export failed: ${errorMessage(e)}` });
        } finally {
            setExporting(null);
        }
    };

    const openPrintable = async () => {
        setExporting('html');
        setExportNote(null);
        try {
            const html = await gatewayClient.exportSystemDocumentationHtml(aiSystemId, docFramework);
            const url = URL.createObjectURL(new Blob([html], { type: 'text/html' }));
            window.open(url, '_blank', 'noopener');
            setTimeout(() => URL.revokeObjectURL(url), 60_000);
            reload();
        } catch (e: unknown) {
            setExportNote({ tone: 'error', text: `Export failed: ${errorMessage(e)}` });
        } finally {
            setExporting(null);
        }
    };

    const profile = data?.profile ?? null;
    const reports = data?.reports ? latestReportPerFramework(data.reports) : null;
    const notice = data?.notice ?? null;
    const lastExport = data?.exports?.find((x) => x.framework_id === docFramework) ?? null;

    return (
        <div className="space-y-5" data-testid="ai-system-compliance-tab">
            <SystemCompliancePanel aiSystemId={aiSystemId} setActiveTab={setActiveTab} />

            {!data ? (
                <p className="text-xs text-muted flex items-center gap-1">
                    <Loader2 className="w-3 h-3 animate-spin" /> Loading reports, documentation and notice…
                </p>
            ) : (
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                    {/* Profile seal state */}
                    <div className={CARD} data-testid="system-compliance-seal">
                        <h4 className={CARD_TITLE}>
                            <ShieldCheck className="w-3.5 h-3.5" /> Profile declaration
                        </h4>
                        {!profile ? (
                            <ReadFailureNotice compact failure={failureSaying(data.failed.profile)} onRetry={reload} />
                        ) : !profile.exists ? (
                            <p className="text-xs text-amber-800">Nothing declared yet, so nothing is sealed.</p>
                        ) : profile.sealed && profile.declaration_record_id ? (
                            <p className="text-xs text-green-800" data-testid="system-compliance-sealed">
                                Sealed as <span className="font-mono">{shortHash(profile.declaration_record_id, 16)}</span>
                                {profile.updated_at ? ` · ${new Date(profile.updated_at).toLocaleString()}` : ''}
                            </p>
                        ) : (
                            <p className="text-xs text-amber-800" data-testid="system-compliance-not-sealed">
                                Saved, not sealed{profile.seal_error ? ` (${profile.seal_error})` : ''} — save the profile again once
                                evidence sealing is available.
                            </p>
                        )}
                    </div>

                    {/* Notice */}
                    <div className={CARD} data-testid="system-compliance-notice">
                        <h4 className={CARD_TITLE}>
                            <Bell className="w-3.5 h-3.5" /> AI-interaction notice
                        </h4>
                        {!notice ? (
                            <ReadFailureNotice compact failure={failureSaying(data.failed.notice)} onRetry={reload} />
                        ) : (
                            <dl className="text-xs space-y-0.5">
                                <div className="flex justify-between gap-2">
                                    <dt className="text-muted">Shown</dt>
                                    <dd className="text-default">{notice.enabled ? `yes — ${notice.surface}` : 'no'}</dd>
                                </div>
                                <div className="flex justify-between gap-2">
                                    <dt className="text-muted">Configured at</dt>
                                    <dd className="text-default">
                                        {notice.source === 'system' ? 'this system' : notice.source === 'tenant' ? 'tenant default' : 'platform default'}
                                    </dd>
                                </div>
                                <div className="flex justify-between gap-2">
                                    <dt className="text-muted">Texts</dt>
                                    <dd className="text-default">
                                        {Object.keys(notice.texts ?? {}).length > 0
                                            ? Object.keys(notice.texts ?? {}).sort().join(', ')
                                            : 'portal default text'}
                                    </dd>
                                </div>
                                {notice.exemption?.asserted && (
                                    <div className="flex justify-between gap-2">
                                        <dt className="text-muted">Exemption</dt>
                                        <dd className="text-default">declared{notice.exemption.basis ? ` — ${notice.exemption.basis}` : ''}</dd>
                                    </div>
                                )}
                                {notice.record_id && (
                                    <div className="flex justify-between gap-2">
                                        <dt className="text-muted">Sealed config</dt>
                                        <dd className="text-default font-mono">{shortHash(notice.record_id, 16)}</dd>
                                    </div>
                                )}
                            </dl>
                        )}
                        {setActiveTab && (
                            <button
                                type="button"
                                className="text-xs text-primary-600 hover:underline mt-1"
                                onClick={() => setActiveTab('compliance-transparency')}
                            >
                                Edit in Compliance → Transparency →
                            </button>
                        )}
                    </div>

                    {/* Reports */}
                    <div className={CARD} data-testid="system-compliance-reports">
                        <h4 className={CARD_TITLE}>
                            <ScrollText className="w-3.5 h-3.5" /> Latest Evidence Reports
                        </h4>
                        {reports === null ? (
                            <ReadFailureNotice compact failure={failureSaying(data.failed.reports)} onRetry={reload} />
                        ) : reports.length === 0 ? (
                            <p className="text-xs text-muted" data-testid="system-compliance-reports-empty">
                                No Evidence Report has been generated for this system yet.
                            </p>
                        ) : (
                            <ul className="text-xs divide-y divide-default">
                                {reports.map((r) => (
                                    <li key={r.id} className="py-1 flex flex-wrap items-center gap-x-2" data-testid={`system-report-${r.framework_id}`}>
                                        <span className="font-medium text-default">{frameworkTitle(r.framework_id)}</span>
                                        <span className="text-secondary">
                                            {r.cadence} · {r.period_start}
                                            {r.period_end && r.period_end !== r.period_start ? ` – ${r.period_end}` : ''}
                                        </span>
                                        <span className={r.status === 'ready' ? 'text-secondary' : r.status === 'failed' ? 'text-red-700' : 'text-amber-800'}>
                                            {r.status}
                                        </span>
                                        <span className="text-muted">catalog {r.catalog_version}</span>
                                        {r.record_id ? (
                                            <span className="font-mono text-muted" title={r.record_id}>
                                                sealed {shortHash(r.record_id, 10)}
                                            </span>
                                        ) : (
                                            <span className="text-amber-800">not sealed</span>
                                        )}
                                        <span className="text-muted">{r.countersignature_status ? (COUNTERSIGNATURE_LABEL[r.countersignature_status] ?? r.countersignature_status) : null}</span>
                                    </li>
                                ))}
                            </ul>
                        )}
                        {setActiveTab && (
                            <button
                                type="button"
                                className="text-xs text-primary-600 hover:underline mt-1"
                                onClick={() => setActiveTab('compliance-reports')}
                            >
                                All reports →
                            </button>
                        )}
                    </div>

                    {/* Documentation */}
                    <div className={CARD} data-testid="system-compliance-documentation">
                        <h4 className={CARD_TITLE}>
                            <FileText className="w-3.5 h-3.5" /> Documentation export
                        </h4>
                        <div className="flex flex-wrap items-center gap-2 text-xs">
                            <select
                                value={docFramework}
                                onChange={(e) => {
                                    setDocFramework(e.target.value);
                                    setExportNote(null);
                                }}
                                className="rounded-md border border-default bg-surface px-2 py-1"
                                aria-label="Documentation framework"
                                data-testid="system-doc-framework"
                            >
                                {data.frameworks.length === 0 && <option value={docFramework}>{docFramework}</option>}
                                {data.frameworks.map((f) => (
                                    <option key={f.id} value={f.id}>
                                        {f.title}
                                    </option>
                                ))}
                            </select>
                            <button
                                type="button"
                                disabled={exporting !== null}
                                onClick={() => void exportJson()}
                                className="inline-flex items-center gap-1 px-2 py-1 rounded-md border border-strong hover:bg-surface-hover disabled:opacity-50"
                                data-testid="system-doc-export"
                            >
                                {exporting === 'json' ? <Loader2 className="w-3 h-3 animate-spin" /> : <Download className="w-3 h-3" />}
                                Export JSON
                            </button>
                            <button
                                type="button"
                                disabled={exporting !== null}
                                onClick={() => void openPrintable()}
                                className="inline-flex items-center gap-1 px-2 py-1 rounded-md border border-strong hover:bg-surface-hover disabled:opacity-50"
                                data-testid="system-doc-printable"
                            >
                                {exporting === 'html' ? <Loader2 className="w-3 h-3 animate-spin" /> : <FileText className="w-3 h-3" />}
                                Printable
                            </button>
                        </div>
                        <p className="text-[11px] text-muted mt-1">
                            Generated from the contract, register, profile and bindings. Each changed document is sealed; an
                            unchanged one returns the previous export.
                        </p>
                        <ReadFailureNotice compact className="block mt-1" failure={failureSaying(data.failed.frameworks)} onRetry={reload} />
                        <ReadFailureNotice compact className="block mt-1" failure={failureSaying(data.failed.exports)} onRetry={reload} />
                        {lastExport && (
                            <p className="text-[11px] text-secondary mt-1" data-testid="system-doc-last">
                                Last export {lastExport.created_at ? new Date(lastExport.created_at).toLocaleString() : ''} · digest{' '}
                                <span className="font-mono">{shortHash(lastExport.digest, 12)}</span>
                                {lastExport.record_id ? ' · sealed' : ' · not sealed'}
                            </p>
                        )}
                        {exportNote && (
                            <p
                                className={`text-[11px] mt-1 ${
                                    exportNote.tone === 'ok' ? 'text-green-800' : exportNote.tone === 'warn' ? 'text-amber-800' : 'text-red-700'
                                }`}
                                data-testid="system-doc-note"
                            >
                                {exportNote.text}
                            </p>
                        )}
                    </div>
                </div>
            )}
        </div>
    );
}

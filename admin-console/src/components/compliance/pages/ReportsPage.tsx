/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Compliance → Reports: Evidence Reports per
 * framework — daily rollups, weekly grades, monthly reports with their
 * bundle. Generate one for a period (idempotent; `force` regenerates in
 * place), open it to read its rows — recomputed, judged (with human
 * agreement beside each), indicator and declared, never merged; the
 * obligations not covered; the stated limits; the catalog version — and
 * download its bundle with the digest the control plane states for it.
 * Countersignature status is passed through as the stored bundle says.
 */

import { useCallback, useState } from 'react';
import { Download, FileText, Loader2, RefreshCw } from 'lucide-react';

import { useGateway } from '../../../hooks/useGateway';
import { useKeyedLoad } from '../../../hooks/useKeyedLoad';
import { errorMessage, getArray, getNumber, getRecord, getString } from '../../../lib/narrow';
import {
    REPORT_CADENCES,
    shortHash,
    type ComplianceReport,
    type ComplianceReportSummary,
    type ReportGenerateResponse,
} from '../../../lib/complianceEvidenceTypes';
import { TABLE_CONTAINER, TABLE_ROW_HOVER, TABLE_TBODY, TABLE_TH, TABLE_THEAD } from '../../ui/tableStyles';
import RecordDialog from '../RecordDialog';
import StatementList from '../StatementList';
import { ErrorNote, Loading, RecordLink, SealNote, Section } from '../ComplianceUi';
import {
    BUTTON_PRIMARY,
    BUTTON_SECONDARY,
    countersignatureLabel,
    downloadJson,
    formatWhen,
    INPUT_CLASS,
    LINK_BUTTON,
    NOT_LEGAL_ADVICE,
    OFFLINE_VERIFIER_URL,
    periodText,
    REPORT_STATUS_CHIP,
} from '../complianceFormat';
import { basisCountsText, groupReportRows, readReportRow, ROW_GROUP_TITLE, ROW_GROUPS } from '../reportRows';
import type { CompliancePageProps } from '../complianceSections';

export default function ReportsPage({ framework, frameworks }: CompliancePageProps) {
    const { gatewayClient, fetchAISystems } = useGateway();
    const [scope, setScope] = useState<string>(''); // '' any · 'estate' · a system id
    const [cadence, setCadence] = useState<string>('');
    const [selectedId, setSelectedId] = useState<string | null>(null);
    const [openRecord, setOpenRecord] = useState<string | null>(null);

    // Generate form
    const [genCadence, setGenCadence] = useState<string>('monthly');
    const [genSystem, setGenSystem] = useState<string>('');
    const [genPeriod, setGenPeriod] = useState<string>('');
    const [genForce, setGenForce] = useState(false);
    const [generating, setGenerating] = useState(false);
    const [genError, setGenError] = useState('');
    const [generated, setGenerated] = useState<ReportGenerateResponse | null>(null);

    const systemsLoader = useCallback(() => fetchAISystems({ limit: 200 }), [fetchAISystems]);
    const systems = useKeyedLoad('systems', systemsLoader).data?.systems ?? [];
    const systemName = (id: string | null | undefined) =>
        id ? (systems.find((s) => s.id === id)?.display_name ?? systems.find((s) => s.id === id)?.name ?? id) : 'estate';

    const loader = useCallback(
        () => gatewayClient.listComplianceReports({ framework, ai_system_id: scope || undefined, cadence: cadence || undefined, limit: 200 }),
        [gatewayClient, framework, scope, cadence],
    );
    const { data, error, loading, reload } = useKeyedLoad(`${framework}:${scope}:${cadence}`, loader, 'Failed to load reports');
    const items = data?.items ?? [];
    const registryEntry = frameworks.find((f) => f.id === framework);

    const generate = async () => {
        setGenError('');
        setGenerated(null);
        setGenerating(true);
        try {
            const res = await gatewayClient.generateComplianceReport({
                framework_id: framework,
                cadence: genCadence,
                ai_system_id: genSystem || null,
                period_start: genPeriod || null,
                force: genForce,
            });
            setGenerated(res);
            setSelectedId(res.report.id);
            reload();
        } catch (e: unknown) {
            setGenError(errorMessage(e, 'The report could not be generated'));
        } finally {
            setGenerating(false);
        }
    };

    return (
        <div className="space-y-4" data-testid="compliance-reports">
            <Section
                title={`Generate — ${registryEntry?.title ?? framework}`}
                subtitle="Idempotent per period: an existing report is returned unless you force a regeneration. Monthly reports are emitted as an evidence bundle rooted at the report record."
                testId="reports-generate"
            >
                <div className="flex flex-wrap items-end gap-2 text-xs">
                    <label className="block">
                        <span className="text-muted uppercase tracking-wide">Cadence</span>
                        <select value={genCadence} onChange={(e) => setGenCadence(e.target.value)} className={`${INPUT_CLASS} block`} data-testid="gen-cadence">
                            {REPORT_CADENCES.map((c) => (
                                <option key={c} value={c}>
                                    {c}
                                </option>
                            ))}
                        </select>
                    </label>
                    <label className="block">
                        <span className="text-muted uppercase tracking-wide">Scope</span>
                        <select value={genSystem} onChange={(e) => setGenSystem(e.target.value)} className={`${INPUT_CLASS} block`} data-testid="gen-system">
                            <option value="">estate-wide</option>
                            {systems.map((s) => (
                                <option key={s.id} value={s.id}>
                                    {s.display_name || s.name}
                                </option>
                            ))}
                        </select>
                    </label>
                    <label className="block">
                        <span className="text-muted uppercase tracking-wide">Any day in the period</span>
                        <input type="date" value={genPeriod} onChange={(e) => setGenPeriod(e.target.value)} className={`${INPUT_CLASS} block`} data-testid="gen-period" />
                    </label>
                    <label className="inline-flex items-center gap-1.5 pb-1.5">
                        <input type="checkbox" checked={genForce} onChange={(e) => setGenForce(e.target.checked)} data-testid="gen-force" />
                        regenerate in place
                    </label>
                    <button type="button" className={BUTTON_PRIMARY} onClick={() => void generate()} disabled={generating} data-testid="gen-run">
                        {generating ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <FileText className="w-3.5 h-3.5" />}
                        Generate
                    </button>
                    <span className="text-muted pb-1.5">Leave the day empty for the last complete period.</span>
                </div>
                {genError && <ErrorNote message={genError} testId="gen-error" />}
                {generated && (
                    <div className="mt-2 space-y-1 text-xs" data-testid="gen-result">
                        <p className="text-secondary">
                            {generated.created ? 'Generated' : generated.regenerated ? 'Regenerated' : 'Already existed — returned as is'}: {generated.report.cadence}{' '}
                            {periodText(generated.report.period_start, generated.report.period_end)} · status {generated.report.status} · catalog{' '}
                            {generated.report.catalog_version}
                        </p>
                        <SealNote seal={generated.seal} what="report" onOpenRecord={setOpenRecord} testId="gen-seal" />
                        {generated.bundle_error && (
                            <p className="text-amber-800" data-testid="gen-bundle-error">
                                Bundle not built: {generated.bundle_error}
                            </p>
                        )}
                        {generated.report.error && <p className="text-red-700">{generated.report.error}</p>}
                    </div>
                )}
            </Section>

            <div className={`${TABLE_CONTAINER} overflow-x-auto`}>
                <div className="flex items-center gap-3 flex-wrap px-4 py-2 border-b border-default text-xs">
                    <label className="inline-flex items-center gap-2">
                        <span className="text-muted uppercase tracking-wide">Scope</span>
                        <select value={scope} onChange={(e) => setScope(e.target.value)} className={INPUT_CLASS} data-testid="reports-scope">
                            <option value="">any</option>
                            <option value="estate">estate-wide only</option>
                            {systems.map((s) => (
                                <option key={s.id} value={s.id}>
                                    {s.display_name || s.name}
                                </option>
                            ))}
                        </select>
                    </label>
                    <label className="inline-flex items-center gap-2">
                        <span className="text-muted uppercase tracking-wide">Cadence</span>
                        <select value={cadence} onChange={(e) => setCadence(e.target.value)} className={INPUT_CLASS} data-testid="reports-cadence">
                            <option value="">any</option>
                            {REPORT_CADENCES.map((c) => (
                                <option key={c} value={c}>
                                    {c}
                                </option>
                            ))}
                        </select>
                    </label>
                    <button type="button" onClick={reload} disabled={loading} className={`${BUTTON_SECONDARY} ml-auto`}>
                        <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} /> Refresh
                    </button>
                </div>
                {error && <ErrorNote message={error} testId="reports-error" />}
                <table className="w-full text-xs">
                    <thead className={TABLE_THEAD}>
                        <tr>
                            <th className={TABLE_TH}>Period</th>
                            <th className={TABLE_TH}>Cadence</th>
                            <th className={TABLE_TH}>Scope</th>
                            <th className={TABLE_TH}>Status</th>
                            <th className={TABLE_TH}>Catalog</th>
                            <th className={TABLE_TH}>Sealed</th>
                            <th className={TABLE_TH}>Countersignature</th>
                            <th className={TABLE_TH}>Generated</th>
                        </tr>
                    </thead>
                    <tbody className={TABLE_TBODY}>
                        {loading && items.length === 0 && (
                            <tr>
                                <td colSpan={8} className="px-4 py-6 text-center text-muted">
                                    <Loader2 className="w-4 h-4 animate-spin inline-block mr-1" /> Loading…
                                </td>
                            </tr>
                        )}
                        {!loading && items.length === 0 && !error && (
                            <tr>
                                <td colSpan={8} className="px-4 py-6 text-center text-muted" data-testid="reports-empty">
                                    No Evidence Report for {registryEntry?.title ?? framework} yet — generate one above, or wait for the daily rollup.
                                </td>
                            </tr>
                        )}
                        {items.map((r) => (
                            <ReportRowView key={r.id} report={r} selected={selectedId === r.id} onSelect={() => setSelectedId(r.id)} systemName={systemName} />
                        ))}
                    </tbody>
                </table>
            </div>

            {selectedId && <ReportDetail key={selectedId} reportId={selectedId} systemName={systemName} onOpenRecord={setOpenRecord} onClose={() => setSelectedId(null)} />}

            {openRecord && <RecordDialog recordId={openRecord} onClose={() => setOpenRecord(null)} />}
        </div>
    );
}

function ReportRowView({
    report: r,
    selected,
    onSelect,
    systemName,
}: {
    report: ComplianceReportSummary;
    selected: boolean;
    onSelect: () => void;
    systemName: (id: string | null | undefined) => string;
}) {
    return (
        <tr className={`${TABLE_ROW_HOVER} cursor-pointer ${selected ? 'bg-surface-alt' : ''}`} onClick={onSelect} data-testid={`report-row-${r.id}`} aria-selected={selected}>
            <td className="px-4 py-2 font-mono text-default whitespace-nowrap">{periodText(r.period_start, r.period_end)}</td>
            <td className="px-4 py-2 text-secondary">{r.cadence}</td>
            <td className="px-4 py-2 text-default">{systemName(r.ai_system_id)}</td>
            <td className="px-4 py-2">
                <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${REPORT_STATUS_CHIP[r.status] ?? 'bg-surface-raised text-secondary'}`}>{r.status}</span>
            </td>
            <td className="px-4 py-2 font-mono text-secondary">{r.catalog_version}</td>
            <td className="px-4 py-2 text-secondary">{r.record_id ? shortHash(r.record_id, 10) : <span className="text-amber-800">not sealed</span>}</td>
            <td className="px-4 py-2 text-secondary">{countersignatureLabel(r.countersignature_status)}</td>
            <td className="px-4 py-2 text-secondary whitespace-nowrap">{formatWhen(r.created_at)}</td>
        </tr>
    );
}

function ReportDetail({
    reportId,
    systemName,
    onOpenRecord,
    onClose,
}: {
    reportId: string;
    systemName: (id: string | null | undefined) => string;
    onOpenRecord: (id: string) => void;
    onClose: () => void;
}) {
    const { gatewayClient } = useGateway();
    const [downloading, setDownloading] = useState(false);
    const [bundleNote, setBundleNote] = useState<{ tone: 'ok' | 'error'; text: string } | null>(null);

    const loader = useCallback((): Promise<ComplianceReport> => gatewayClient.getComplianceReport(reportId), [gatewayClient, reportId]);
    const { data: report, error, loading } = useKeyedLoad(`report:${reportId}`, loader, 'Failed to load the report');

    const downloadBundle = async () => {
        setDownloading(true);
        setBundleNote(null);
        try {
            const { bundle, digest } = await gatewayClient.getComplianceReportBundle(reportId);
            downloadJson(bundle, `evidence-report-${reportId}.json`);
            setBundleNote({ tone: 'ok', text: `Bundle downloaded · digest ${digest ?? 'not stated by the control plane'}` });
        } catch (e: unknown) {
            setBundleNote({ tone: 'error', text: errorMessage(e, 'The bundle could not be fetched') });
        } finally {
            setDownloading(false);
        }
    };

    if (loading && !report) return <Loading label="Loading the report…" testId="report-loading" />;
    if (error) return <ErrorNote message={error} testId="report-error" />;
    if (!report) return null;

    const rows = (report.rows ?? []).map(readReportRow).filter((r): r is NonNullable<typeof r> => r !== null);
    const groups = groupReportRows(rows);
    const summary = report.summary;
    const limits = (getArray(summary, 'stated_limits') ?? []).filter((l): l is string => typeof l === 'string');
    const witness = getRecord(summary, 'witness_plurality');
    const runsSealed = getNumber(summary, 'runs_sealed');
    const headGrade = getString(summary, 'head_grade') ?? getString(witness, 'grade');
    const bundleError = getString(summary, 'bundle_error');

    return (
        <Section
            title={`Evidence Report · ${report.cadence} ${periodText(report.period_start, report.period_end)} · ${systemName(report.ai_system_id)}`}
            subtitle={`${report.framework_id} · catalog ${report.catalog_version} · ${NOT_LEGAL_ADVICE}`}
            actions={
                <>
                    {report.bundle_id ? (
                        <button type="button" className={BUTTON_SECONDARY} onClick={() => void downloadBundle()} disabled={downloading} data-testid="report-bundle-download">
                            {downloading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Download className="w-3.5 h-3.5" />}
                            Download bundle
                        </button>
                    ) : (
                        <span className="text-xs text-muted" data-testid="report-no-bundle">
                            no bundle{bundleError ? ` (${bundleError})` : report.cadence === 'monthly' ? '' : ' — bundles are built for monthly reports'}
                        </span>
                    )}
                    <button type="button" className={LINK_BUTTON} onClick={onClose} data-testid="report-close">
                        Close
                    </button>
                </>
            }
            testId="report-detail"
        >
            <div className="space-y-4 text-xs">
                <dl className="grid grid-cols-2 md:grid-cols-4 gap-x-6 gap-y-2" data-testid="report-facts">
                    <div>
                        <dt className="text-muted uppercase tracking-wide">Status</dt>
                        <dd className="text-default">{report.status}{report.error ? ` — ${report.error}` : ''}</dd>
                    </div>
                    <div>
                        <dt className="text-muted uppercase tracking-wide">Sealed record</dt>
                        <dd>{report.record_id ? <RecordLink recordId={report.record_id} onOpen={onOpenRecord} /> : <span className="text-amber-800">not sealed</span>}</dd>
                    </div>
                    <div>
                        <dt className="text-muted uppercase tracking-wide">Countersignature</dt>
                        <dd className="text-default" data-testid="report-countersignature">
                            {countersignatureLabel(report.countersignature_status)}
                            {(report.countersignatures ?? []).length > 0 ? ` (${(report.countersignatures ?? []).length})` : ''}
                        </dd>
                    </div>
                    <div>
                        <dt className="text-muted uppercase tracking-wide">Permalink</dt>
                        <dd className="font-mono text-secondary break-all" data-testid="report-permalink">
                            {report.permalink ?? '—'}
                        </dd>
                    </div>
                    <div>
                        <dt className="text-muted uppercase tracking-wide">Obligations in scope</dt>
                        <dd className="text-default">{getNumber(summary, 'obligations_in_scope') ?? '—'}</dd>
                    </div>
                    <div>
                        <dt className="text-muted uppercase tracking-wide">Statements</dt>
                        <dd className="text-default">{getNumber(summary, 'statements') ?? rows.length}</dd>
                    </div>
                    <div>
                        <dt className="text-muted uppercase tracking-wide">Runs sealed</dt>
                        <dd className="text-default">{runsSealed ?? '—'}</dd>
                    </div>
                    <div>
                        <dt className="text-muted uppercase tracking-wide">Head grade</dt>
                        <dd className="text-default">{headGrade ?? '—'}</dd>
                    </div>
                </dl>

                <p className="text-secondary" data-testid="report-basis-counts">
                    {ROW_GROUPS.filter((g) => g !== 'not_covered').map((g) => {
                        const text = basisCountsText(getRecord(summary, g));
                        return text ? (
                            <span key={g} className="mr-3">
                                <span className="uppercase font-semibold">{g}</span> {text}
                            </span>
                        ) : null;
                    })}
                    {groups.not_covered.length > 0 && <span className="text-amber-800">not covered {groups.not_covered.length}</span>}
                </p>

                {bundleNote && (
                    <p className={bundleNote.tone === 'ok' ? 'text-secondary' : 'text-red-700'} data-testid="report-bundle-note">
                        {bundleNote.text}
                        {bundleNote.tone === 'ok' && (
                            <>
                                {' '}
                                ·{' '}
                                <a href={OFFLINE_VERIFIER_URL} target="_blank" rel="noopener noreferrer" className="text-primary-600 hover:underline">
                                    Verify offline →
                                </a>
                            </>
                        )}
                    </p>
                )}

                {ROW_GROUPS.map((g) => {
                    const list = groups[g];
                    if (list.length === 0) return null;
                    return (
                        <div key={g} data-testid={`report-group-${g}`}>
                            <h4 className="text-[10px] font-semibold text-muted uppercase tracking-wide mb-1.5">
                                {ROW_GROUP_TITLE[g]} ({list.length})
                            </h4>
                            {g === 'not_covered' ? (
                                <ul className="space-y-1">
                                    {list.map((r) => (
                                        <li key={`${r.ai_system_id}:${r.obligation_key}`} className="text-amber-800" data-testid={`report-not-covered-${r.ai_system_id}-${r.obligation_key}`}>
                                            {r.ai_system_name} · {r.obligation_title} {r.reference} — {r.detail ?? 'no evidence statement in the catalog'}
                                        </li>
                                    ))}
                                </ul>
                            ) : (
                                <div className="space-y-2">
                                    {list.map((r, i) => (
                                        <div key={`${r.ai_system_id}:${r.obligation_key}:${r.key}:${r.catalog_version ?? ''}:${i}`}>
                                            <p className="text-secondary mb-1">
                                                <span className="text-default font-medium">{r.ai_system_name}</span> · {r.obligation_title}{' '}
                                                <span className="text-muted">{r.reference}</span>
                                                {r.obligation_limit && <span className="block text-amber-800">Obligation limit: {r.obligation_limit}</span>}
                                            </p>
                                            <StatementList statements={[r]} onOpenRecord={onOpenRecord} testId={`report-rows-${g}-${r.ai_system_id}-${r.obligation_key}`} />
                                        </div>
                                    ))}
                                </div>
                            )}
                        </div>
                    );
                })}

                {rows.length === 0 && (
                    <p className="text-muted" data-testid="report-no-rows">
                        The report has no rows — no obligation was in force for a system in scope during the period.
                    </p>
                )}

                {limits.length > 0 && (
                    <div data-testid="report-limits">
                        <h4 className="text-[10px] font-semibold text-muted uppercase tracking-wide mb-1">Stated limits</h4>
                        <ul className="list-disc pl-4 text-amber-800 space-y-0.5">
                            {limits.map((l) => (
                                <li key={l}>{l}</li>
                            ))}
                        </ul>
                    </div>
                )}
            </div>
        </Section>
    );
}

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Compliance → Overview.
 *
 * Per framework: readiness per AI System (profile declared and sealed;
 * obligations in force and upcoming, dated; statement counts by basis —
 * recomputed and judged side by side, never summed; the tenant log's latest
 * head grade; the latest report), the framework's dated timeline,
 * undetermined declarations as a to-do list, witness health and record lag.
 *
 * Counts, never verdicts: nothing here says a system "is" anything.
 */

import { useCallback, useState } from 'react';
import { AlertTriangle, CalendarClock, Clock, Library, ShieldCheck } from 'lucide-react';

import { useGateway } from '../../../hooks/useGateway';
import { useKeyedLoad } from '../../../hooks/useKeyedLoad';
import { errorMessage, getNumber, getString } from '../../../lib/narrow';
import {
    chipFor,
    HEAD_GRADE_CHIP,
    headGradeLabel,
    type ComplianceOverviewResponse,
    type ComplianceOverviewSystem,
    type EvidenceSummary,
} from '../../../lib/complianceEvidenceTypes';
import { TABLE_CONTAINER, TABLE_TBODY, TABLE_TH, TABLE_THEAD } from '../../ui/tableStyles';
import {
    CompositeClassificationNote,
    ErrorNote,
    EvaluatedTier,
    Loading,
    Section,
    StatusChip,
    WindowPicker,
} from '../ComplianceUi';
import { byBasisText, formatDay, formatDelay, LINK_BUTTON, NOT_LEGAL_ADVICE, windowFor, type WindowPreset } from '../complianceFormat';
import type { CompliancePageProps } from '../complianceSections';

interface OverviewData {
    overview: ComplianceOverviewResponse | null;
    overviewError: string;
    summary: EvidenceSummary | null;
    summaryError: string;
}

export default function OverviewPage({ framework, frameworks, onTabChange, setActiveTab }: CompliancePageProps) {
    const { gatewayClient } = useGateway();
    const [preset, setPreset] = useState<WindowPreset>('30d');

    const registryEntry = frameworks.find((f) => f.id === framework);
    const hasCatalog = (registryEntry?.obligation_count ?? 0) > 0;

    // Two independent sources; each fails on its own so the other renders.
    const loader = useCallback(async (): Promise<OverviewData> => {
        const [o, s] = await Promise.allSettled([
            hasCatalog ? gatewayClient.getComplianceOverview(framework, windowFor(preset)) : Promise.resolve(null),
            gatewayClient.getEvidenceSummary(),
        ]);
        return {
            overview: o.status === 'fulfilled' ? o.value : null,
            overviewError: o.status === 'rejected' ? errorMessage(o.reason, 'Failed to load the overview') : '',
            summary: s.status === 'fulfilled' ? s.value : null,
            summaryError: s.status === 'rejected' ? errorMessage(s.reason, 'Failed to load the evidence summary') : '',
        };
    }, [gatewayClient, framework, hasCatalog, preset]);
    const { data, loading } = useKeyedLoad(`${framework}:${hasCatalog}:${preset}`, loader);

    if (loading && !data) return <Loading testId="overview-loading" />;

    const overview = data?.overview ?? null;
    const summary = data?.summary ?? null;
    const log = summary?.log ?? null;
    const evidence = overview?.evidence;
    const lagP50 = getNumber(evidence?.record_lag, 'p50_seconds');
    const lagMax = getNumber(evidence?.record_lag, 'max_seconds');
    const lagRecords = getNumber(evidence?.record_lag, 'records');
    const grade = evidence?.grade ?? log?.last_tree_head?.grade ?? null;
    const enabledCount = frameworks.filter((f) => f.enabled).length;
    const systems = overview?.systems ?? [];
    const undetermined = systems.flatMap((s) => s.undetermined.map((u) => ({ system: s, ...u })));

    return (
        <div className="space-y-5" data-testid="compliance-overview">
            <ErrorNote message={data?.summaryError ?? ''} testId="overview-summary-error" />

            <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
                <Card title="Witness health" icon={ShieldCheck} testId="card-witness">
                    {grade ? (
                        <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${chipFor(HEAD_GRADE_CHIP, grade)}`} data-testid="overview-grade">
                            latest head {headGradeLabel(grade)}
                        </span>
                    ) : (
                        <p className="text-xs text-muted">No tree head sealed yet.</p>
                    )}
                    <p className="text-xs mt-2" data-testid="overview-unwitnessed">
                        {log ? (
                            (log.unwitnessed_heads ?? 0) > 0 ? (
                                <span className="text-amber-800">
                                    {log.unwitnessed_heads} unwitnessed head{log.unwitnessed_heads === 1 ? '' : 's'}
                                </span>
                            ) : (
                                <span className="text-secondary">no unwitnessed heads</span>
                            )
                        ) : (
                            <span className="text-muted">no tenant log yet</span>
                        )}
                    </p>
                </Card>
                <Card title="Record lag" icon={Clock} testId="card-lag">
                    <p className="text-2xl font-semibold text-default" data-testid="overview-lag">
                        {lagP50 !== undefined ? formatDelay(lagP50) : '—'}
                    </p>
                    <p className="text-xs text-muted">
                        median seal delay{lagMax !== undefined ? ` · max ${formatDelay(lagMax)}` : ''}
                        {lagRecords !== undefined ? ` · ${lagRecords.toLocaleString()} records` : ''}
                    </p>
                </Card>
                <Card title="Capture gaps" icon={AlertTriangle} testId="card-capture">
                    <p className="text-2xl font-semibold text-default" data-testid="overview-capture-gaps">
                        {evidence ? evidence.open_capture_gap_items : '—'}
                    </p>
                    <p className="text-xs text-muted">open capture-gap items in the inbox</p>
                    {setActiveTab && (
                        <button type="button" className={`${LINK_BUTTON} mt-1`} onClick={() => setActiveTab('inbox')}>
                            Open the inbox →
                        </button>
                    )}
                </Card>
                <Card title="Frameworks" icon={Library} testId="card-frameworks">
                    <p className="text-2xl font-semibold text-default">
                        {enabledCount} <span className="text-sm font-normal text-muted">of {frameworks.length} enabled</span>
                    </p>
                    <button
                        type="button"
                        className={`${LINK_BUTTON} mt-1`}
                        onClick={() => onTabChange('compliance-frameworks')}
                        data-testid="overview-frameworks-link"
                    >
                        Manage frameworks →
                    </button>
                </Card>
            </div>

            {!hasCatalog && (
                <div className="rounded-lg border border-default bg-surface p-6 text-center text-sm" data-testid="overview-no-catalog">
                    <p className="text-default font-medium">{registryEntry?.title ?? framework} has no obligation catalog.</p>
                    <p className="text-xs text-secondary mt-1">
                        Nothing is evaluated for it, and nothing on this page pretends otherwise.
                    </p>
                </div>
            )}

            {hasCatalog && <ErrorNote message={data?.overviewError ?? ''} testId="overview-error" />}

            {overview && (
                <>

                    <Section
                        title={`${overview.framework.title} — readiness per AI System`}
                        subtitle={`Catalog ${overview.framework.catalog_version ?? '—'} · window ${formatDay(overview.window.from)} → ${formatDay(overview.window.to)} · ${NOT_LEGAL_ADVICE}`}
                        actions={<WindowPicker value={preset} onChange={setPreset} testId="overview-window" />}
                        testId="overview-readiness"
                    >
                        {overview.framework.composite_classification && (
                            <div className="mb-3">
                                <CompositeClassificationNote testId="overview-composite-classification" />
                            </div>
                        )}
                        <ReadinessTable
                            systems={systems}
                            grade={grade}
                            onOpenSystem={() => onTabChange('compliance-profiles')}
                            onOpenReports={() => onTabChange('compliance-reports')}
                        />
                    </Section>

                    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                        <Section
                            title="Undetermined declarations"
                            subtitle="An obligation whose applicability rests on a fact nobody declared yet. Declare it and the row recomputes."
                            testId="overview-undetermined"
                        >
                            {undetermined.length === 0 ? (
                                <p className="text-xs text-muted" data-testid="overview-undetermined-empty">
                                    Nothing undetermined for this framework.
                                </p>
                            ) : (
                                <ul className="space-y-1.5">
                                    {undetermined.map((u) => (
                                        <li key={`${u.system.ai_system_id}:${u.key}`} className="text-xs flex items-start gap-2" data-testid={`undetermined-${u.system.ai_system_id}-${u.key}`}>
                                            <StatusChip status="undetermined" />
                                            <span className="min-w-0">
                                                <span className="text-default font-medium">{u.system.name}</span> · {u.title}
                                                {u.reason && <span className="block text-muted">{u.reason}</span>}
                                            </span>
                                            <button type="button" className={`${LINK_BUTTON} ml-auto flex-none`} onClick={() => onTabChange('compliance-profiles')}>
                                                Declare →
                                            </button>
                                        </li>
                                    ))}
                                </ul>
                            )}
                        </Section>

                        <Section title="Timeline" subtitle="When each obligation starts to bind — dated, never a checkbox." testId="overview-timeline-section">
                            <ol className="divide-y divide-default" data-testid="overview-timeline">
                                {overview.timeline.map((o) => (
                                    <li key={o.key} className="flex items-center gap-3 py-1.5 text-xs" data-testid={`timeline-${o.key}`}>
                                        <CalendarClock className={`w-3.5 h-3.5 flex-none ${o.in_force ? 'text-green-700' : 'text-muted'}`} />
                                        <span className="font-mono text-secondary w-24 flex-none">{o.applies_from}</span>
                                        <span className="text-default flex-1 min-w-0 truncate" title={o.title}>
                                            {o.title}
                                        </span>
                                        <span
                                            className={`flex-none px-1.5 py-0.5 rounded text-[10px] font-semibold ${
                                                o.in_force ? 'bg-blue-100 text-blue-800' : 'bg-surface-raised text-secondary'
                                            }`}
                                        >
                                            {o.in_force ? 'in force' : 'upcoming'}
                                        </span>
                                    </li>
                                ))}
                            </ol>
                        </Section>
                    </div>
                </>
            )}
        </div>
    );
}

function ReadinessTable({
    systems,
    grade,
    onOpenSystem,
    onOpenReports,
}: {
    systems: ComplianceOverviewSystem[];
    grade: string | null;
    onOpenSystem: () => void;
    onOpenReports: () => void;
}) {
    if (systems.length === 0) {
        return (
            <p className="text-xs text-muted" data-testid="overview-no-systems">
                No active AI System — create one under AI Estate → AI Systems.
            </p>
        );
    }
    return (
        <div className={`${TABLE_CONTAINER} overflow-x-auto`}>
            <table className="w-full text-xs">
                <thead className={TABLE_THEAD}>
                    <tr>
                        <th className={TABLE_TH}>AI System</th>
                        <th className={TABLE_TH}>Profile</th>
                        <th className={TABLE_TH}>In force</th>
                        <th className={TABLE_TH}>Upcoming</th>
                        <th className={TABLE_TH}>Statements by basis</th>
                        <th className={TABLE_TH}>Head grade</th>
                        <th className={TABLE_TH}>Capture</th>
                        <th className={TABLE_TH}>Latest report</th>
                    </tr>
                </thead>
                <tbody className={TABLE_TBODY}>
                    {systems.map((s) => {
                        const next = s.obligations_upcoming[0];
                        const report = s.latest_report;
                        const unsealed = s.capture.unsealed_governed_actions;
                        return (
                            <tr key={s.ai_system_id} data-testid={`readiness-${s.ai_system_id}`}>
                                <td className="px-4 py-2.5">
                                    <button type="button" className="font-medium text-primary-600 hover:underline text-left" onClick={onOpenSystem}>
                                        {s.name}
                                    </button>
                                    <div className="text-muted">
                                        <EvaluatedTier
                                            declared={s.risk_tier_declared ?? s.risk_tier}
                                            evaluated={s.risk_tier}
                                            sources={s.effective_tier_sources}
                                            testId={`readiness-tier-${s.ai_system_id}`}
                                        />{' '}
                                        · {s.role ?? 'role not declared'}
                                    </div>
                                </td>
                                <td className="px-4 py-2.5" data-testid={`readiness-profile-${s.ai_system_id}`}>
                                    {!s.profile_declared ? (
                                        <span className="text-amber-800">not declared</span>
                                    ) : s.profile_sealed ? (
                                        <span className="text-secondary">declared · sealed</span>
                                    ) : (
                                        <span className="text-amber-800">declared · not sealed</span>
                                    )}
                                </td>
                                <td className="px-4 py-2.5 text-default" title={s.obligations_in_force.map((o) => o.title).join('\n')}>
                                    {s.obligations_in_force.length}
                                </td>
                                <td className="px-4 py-2.5 text-secondary">
                                    {s.obligations_upcoming.length}
                                    {next && <span className="block text-muted">next {next.applies_from}</span>}
                                </td>
                                <td className="px-4 py-2.5" data-testid={`readiness-statements-${s.ai_system_id}`}>
                                    <div className="flex items-center gap-1.5 flex-wrap">
                                        {s.statement_counts.worst_status && <StatusChip status={s.statement_counts.worst_status} />}
                                        <span className="text-secondary">{byBasisText(s.statement_counts.by_basis) || '—'}</span>
                                    </div>
                                </td>
                                <td className="px-4 py-2.5">
                                    {grade ? (
                                        <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${chipFor(HEAD_GRADE_CHIP, grade)}`}>
                                            {headGradeLabel(grade)}
                                        </span>
                                    ) : (
                                        <span className="text-muted">—</span>
                                    )}
                                </td>
                                <td className="px-4 py-2.5 text-secondary">
                                    {unsealed === null || unsealed === undefined ? (
                                        <span className="text-muted">not evaluable</span>
                                    ) : unsealed > 0 ? (
                                        <span className="text-amber-800">{unsealed.toLocaleString()} unsealed</span>
                                    ) : (
                                        'none unsealed'
                                    )}
                                    {(s.capture.open_capture_gap_items ?? 0) > 0 && (
                                        <span className="block text-amber-800">{s.capture.open_capture_gap_items} gap item(s)</span>
                                    )}
                                </td>
                                <td className="px-4 py-2.5">
                                    {report ? (
                                        <button type="button" className={LINK_BUTTON} onClick={onOpenReports}>
                                            {getString(report, 'cadence') ?? 'report'} {getString(report, 'period_start') ?? ''} ·{' '}
                                            {getString(report, 'status') ?? '—'}
                                        </button>
                                    ) : (
                                        <span className="text-muted">none yet</span>
                                    )}
                                </td>
                            </tr>
                        );
                    })}
                </tbody>
            </table>
        </div>
    );
}

function Card({
    title,
    icon: Icon,
    testId,
    children,
}: {
    title: string;
    icon: React.FC<{ className?: string }>;
    testId: string;
    children: React.ReactNode;
}) {
    return (
        <div className="rounded-lg border border-default bg-surface p-4" data-testid={testId}>
            <div className="flex items-center gap-2 mb-2">
                <Icon className="w-4 h-4 text-primary-600" />
                <h3 className="text-xs font-semibold text-muted uppercase tracking-wide">{title}</h3>
            </div>
            {children}
        </div>
    );
}

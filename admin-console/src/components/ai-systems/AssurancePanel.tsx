/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Health score and the Assurance Report.
 *
 * **Only `healthy` is green.** `learning` and `unknown` render amber, not a
 * muted grey that reads as "fine". An optimistic UI rounds those two up, and
 * they are precisely the states where the platform has *not measured*
 * something — which must never look like a clean bill of health.
 *
 * **The score is always decomposable.** One number on the tile, five
 * components underneath, because a score nobody can take apart is a score
 * nobody trusts. Chain integrity sits next to it: if only 60% of a system's
 * runs were observed intact, the operator has to know that before deciding
 * what the number is worth.
 *
 * **`not_covered` is rendered, not hidden behind a disclaimer link.** It is
 * the section that makes the rest of the report credible, and burying it
 * would be the exact over-claim the report exists to prevent.
 */

import { useCallback, useEffect, useState } from 'react';
import {
    AlertTriangle,
    Camera,
    Download, Printer,
    RefreshCw,
    ShieldCheck,
} from 'lucide-react';

import { HelpTip } from '../ui/HelpTip';
import AssuranceLifecycleDiagram from './AssuranceLifecycleDiagram';
import { DependencyCapBanner, DependencyHealthList, SignalBasisNote } from './DependencyHealthList';
import { CompositionReportSection } from './CompositionReportSection';
import { baselineValue, distributionSummary } from '../../lib/baselineFormat';
import { readCompositionSection } from '../../lib/compositionReport';
import { JudgementExclusionsSection } from './JudgementExclusionsSection';
import {
    epochInForce,
    epochText,
    readExcludedPeriod,
    readJudgementExclusions,
} from '../../lib/driftResolution';
import type { BaselineEpoch } from '../../lib/driftResolutionTypes';

import { useGateway } from '../../hooks/useGateway';
import {
    AssuranceHistory,
    DependencyHealthEntry,
    EnvelopeEvaluation,
    SIGNAL_BASIS_LABELS,
    ServiceFacts,
    SignalBasis,
    HEALTH_CLASSES,
    HEALTH_COMPONENTS,
    HEALTH_LABELS,
    ReportVerdict,
    SystemHealth,
    VERDICT_CLASSES,
    VERDICT_LABELS, driftMetricLabel, envelopeTermStatusLabel
} from '../../lib/aiSystemTypes';
import { dependencyLink } from '../../lib/dependencyHealth';
import { notApplicableText } from '../../lib/signalsOverview';
import { downloadJson } from '../../lib/downloadJson';
import { printAssuranceReport } from '../../lib/printableDocuments';
import { errorKind } from '../../lib/narrow';

/** Report spec 1.1 — the learned-baselines section. */
interface BaselineRow {
    metric_key: string;
    /** Own runs, or served calls as a shared service. */
    basis?: SignalBasis;
    state: string;
    sample_count: number;
    median: number | null;
    mad: number | null;
    p90: number | null;
    distribution: Record<string, number> | null;
}

interface BaselineSection {
    note?: string;
    ready: number;
    learning: number;
    metrics: BaselineRow[];
    sub_baselines?: { seasonal: number; per_actor: number; per_caller?: number };
}

/** Report spec 1.1 — findings turned into named next controls. */
interface RecommendedControl {
    control: string;
    severity: 'high' | 'medium' | 'low';
    reason: string;
    subject?: string;
}

const CONTROL_LABELS: Record<string, string> = {
    add_response_policy: 'Add a response policy',
    report_run_ends: 'Report run ends from the client',
    approve_contract: 'Approve the declared envelope',
    mint_contract: 'Mint a contract',
    declare_or_block_client: 'Declare or block client',
    revoke_unused_grants: 'Revoke unused grants',
    bind_replay_suite: 'Bind a replay suite',
    sign_run_chains: 'Sign run chains from the SDK',
    configure_liveness: 'Configure a liveness expectation',
    attach_impact_assessment: 'Attach an impact assessment',
    attach_independent_evidence: 'Attach independent evidence',
};

const REC_SEVERITY_CLASSES: Record<RecommendedControl['severity'], string> = {
    high: 'bg-red-100 text-red-800',
    medium: 'bg-amber-100 text-amber-800',
    low: 'bg-slate-100 text-slate-600',
};

/** Report spec 1.2/1.3 — the executive layer. */
interface AssuranceSummary {
    conclusion: string;
    /** 1.3 — the word a decision-maker reads first. */
    /** The label carries its basis; `basis_note` is the sentence under the badge. */
    status?: { code: string; label: string; basis_note?: string };
    what_we_know?: string;
    what_prevents_full_assurance?: string[];
    recommended_action?: string;
    /** 1.4 — the risk interpretation in one or two sentences. */
    why_it_matters?: string;
    level: {
        level: number;
        /** 1.3 — "Evidence maturity": the number measures evidence, not trust. */
        scale?: string;
        label: string;
        means: string;
        next?: {
            level: number;
            label: string;
            means: string;
            /** What this rung requires. */
            requires?: string;
            /** The evaluated dimension's own detail — why the rung is unmet.
             *  Read from the dimension rather than restated, so it cannot
             *  drift from the logic that decided the rung was not held. */
            blocker?: string;
            status?: string;
            dimension?: string;
            /** The one thing an operator does to earn the rung. */
            action?: string;
            surface?: string;
        } | null;
        /** Every rung with its requirement and whether it is held, so the
         *  ladder renders as a checklist rather than a single number. */
        rungs?: {
            level: number;
            label: string;
            held: boolean;
            requires: string;
            dimension: string;
            status?: string;
        }[];
    };
    dimensions: Record<string, { status: 'ok' | 'partial' | 'missing'; detail: string }>;
}

const STATUS_CLASSES: Record<string, string> = {
    assured: 'bg-green-100 text-green-800',
    assured_with_exceptions: 'bg-yellow-100 text-yellow-800',
    conditionally_assured: 'bg-amber-100 text-amber-900',
    violations: 'bg-red-100 text-red-800',
    insufficient_evidence: 'bg-slate-100 text-slate-600',
};

const DIMENSION_LABELS: Record<string, string> = {
    operational_evidence: 'Operational evidence',
    behavioural_stability: 'Behavioural stability',
    contract_assurance: 'Contract assurance',
    execution_evidence: 'Execution evidence',
    independent_evidence: 'Independent evidence',
};

const DIMENSION_STATUS: Record<string, { glyph: string; cls: string }> = {
    ok: { glyph: '✓', cls: 'bg-green-100 text-green-800' },
    partial: { glyph: '△', cls: 'bg-amber-100 text-amber-800' },
    missing: { glyph: '✕', cls: 'bg-red-100 text-red-800' },
};

export type AssuranceSubTab = 'conclusion' | 'evidence' | 'findings' | 'snapshots';

const SUB_TABS: { id: AssuranceSubTab; label: string }[] = [
    { id: 'conclusion', label: 'Conclusion' },
    { id: 'evidence', label: 'Evidence' },
    { id: 'findings', label: 'Exceptions & changes' },
    { id: 'snapshots', label: 'Snapshots' },
];

interface AssurancePanelProps {
    aiSystemId: string;
    /** Which sub-tab to open on. */
    initialSubTab?: AssuranceSubTab;
    /** Navigate elsewhere in the console (the loop diagram's findings → Inbox). */
    setActiveTab?: (tab: string) => void;
}

export function AssurancePanel({ aiSystemId, initialSubTab, setActiveTab }: AssurancePanelProps) {
    const [sub, setSub] = useState<AssuranceSubTab>(initialSubTab ?? 'conclusion');
    const {
        fetchSystemHealth,
        fetchAssuranceReport,
        captureAssuranceReport,
        fetchAssuranceHistory,
        exportAssuranceCertificate,
    } = useGateway();

    const [health, setHealth] = useState<SystemHealth | null>(null);
    const [report, setReport] = useState<Record<string, unknown> | null>(null);
    const [history, setHistory] = useState<AssuranceHistory | null>(null);
    const [loading, setLoading] = useState(true);
    const [busy, setBusy] = useState('');
    const [error, setError] = useState('');

    const load = useCallback(async () => {
        try {
            setLoading(true);
            setError('');
            const [h, r, hist] = await Promise.all([
                fetchSystemHealth(aiSystemId),
                fetchAssuranceReport(aiSystemId),
                fetchAssuranceHistory(aiSystemId),
            ]);
            setHealth(h);
            setReport(r);
            setHistory(hist);
        } catch (err: unknown) {
            console.error('Failed to load assurance:', errorKind(err));
            setError(err instanceof Error ? err.message : 'Failed to load');
            setHealth(null);
            setReport(null);
        } finally {
            setLoading(false);
        }
    }, [aiSystemId, fetchSystemHealth, fetchAssuranceReport, fetchAssuranceHistory]);

    useEffect(() => {
        void load();
    }, [load]);

    const capture = async () => {
        try {
            setBusy('capture');
            setError('');
            await captureAssuranceReport(aiSystemId);
            await load();
        } catch (err: unknown) {
            setError(err instanceof Error ? err.message : 'Could not capture');
        } finally {
            setBusy('');
        }
    };

    const download = () => {
        if (!report) return;
        // Export is a plain JSON download rather than a server round-trip:
        // it is exactly the document already on screen, so anything else
        // risks the exported artifact differing from the reviewed one.
        downloadJson(`assurance-${aiSystemId}.json`, report);
    };

    const downloadCertificate = async (reportId: string, asPdf = false) => {
        try {
            setBusy(`cert-${reportId}`);
            const cert = await exportAssuranceCertificate(aiSystemId, reportId);
            if (asPdf) {
                printAssuranceReport(
                    cert.report as Record<string, unknown>,
                    { attestation: cert.attestation as Record<string, unknown> }
                );
            } else {
                downloadJson(`assurance-certificate-${reportId}.json`, cert);
            }
        } catch (err: unknown) {
            setError(
                err instanceof Error ? err.message : 'Could not export certificate'
            );
        } finally {
            setBusy('');
        }
    };

    const notCovered = (report?.not_covered as string[] | undefined) ?? [];
    const verdict = report?.verdict as ReportVerdict | undefined;
    const compliance = report?.contract_compliance as
        | {
              under_signature: boolean;
              envelope?: EnvelopeEvaluation | null;
              config_drifted?: boolean;
              unsigned_declaration?: string;
          }
        | undefined;
    const exceptions = report?.exceptions as Record<string, number> | undefined;
    const changes = report?.changes as Record<string, unknown> | undefined;
    const baselines = report?.baselines as BaselineSection | undefined;
    const recommendations =
        (report?.recommended_controls as RecommendedControl[] | undefined) ?? [];
    const summary = report?.assurance_summary as AssuranceSummary | undefined;
    // What the system served as a shared service (null unless its
    // signals are on the segment basis), and its dependencies' health.
    const service = (report?.service as ServiceFacts | null | undefined) ?? null;
    const dependencyHealth =
        (report?.dependency_health as DependencyHealthEntry[] | undefined) ?? [];
    // Every verdict names its failing dependencies (none: an empty list).
    const failingDependencies = verdict ? verdict.failing_dependencies : [];
    // Null where the report carries no composition section.
    const composition = readCompositionSection(report);
    // Null where the report carries no judgement-exclusions section.
    const exclusions = readJudgementExclusions(report);
    const excludedPeriod = readExcludedPeriod(report);
    // Either basis means the gateway could not assure against a signed bound:
    // no envelope in force, or every run in the period was ingested
    // telemetry the gateway never witnessed.
    const conditional = verdict?.basis === 'observed_history' || verdict?.basis === 'reported_telemetry';

    return (
        <div className="space-y-4" data-testid="assurance-panel">
            <div className="flex items-start justify-between gap-4">
                <div className="flex items-start gap-2">
                    <h3 className="text-sm font-semibold text-default">Assurance</h3>
                    <HelpTip text="One page answering the four guarantees with evidence — and stating what the platform did not observe." />
                </div>
                <div className="flex items-center gap-2">
                    <button
                        type="button"
                        disabled={busy === 'capture'}
                        onClick={() => void capture()}
                        className="flex items-center gap-2 px-3 py-1.5 text-sm border border-strong rounded-lg hover:bg-surface-hover disabled:opacity-50"
                        data-testid="capture-button"
                    >
                        <Camera className="w-4 h-4" />
                        Capture snapshot
                    </button>
                    <button
                        type="button"
                        disabled={!report}
                        onClick={download}
                        className="flex items-center gap-2 px-3 py-1.5 text-sm border border-strong rounded-lg hover:bg-surface-hover disabled:opacity-50"
                        data-testid="export-button"
                    >
                        <Download className="w-4 h-4" />
                        Export JSON
                    </button>
                    <button
                        type="button"
                        disabled={!report}
                        onClick={() => report && printAssuranceReport(report)}
                        title="Print / save as PDF — a readable rendering of the same canonical report"
                        className="flex items-center gap-2 px-3 py-1.5 text-sm bg-primary-600 hover:bg-primary-700 text-white rounded-lg disabled:opacity-50"
                        data-testid="export-pdf-button"
                    >
                        <Printer className="w-4 h-4" />
                        Export PDF
                    </button>
                    <button
                        type="button"
                        onClick={() => void load()}
                        className="flex items-center gap-2 px-3 py-1.5 text-sm border border-strong rounded-lg hover:bg-surface-hover"
                    >
                        <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
                        Refresh
                    </button>
                </div>
            </div>

            {error && (
                <div className="flex items-start gap-2 p-3 border border-red-200 bg-red-50 rounded">
                    <AlertTriangle className="w-4 h-4 text-red-600 shrink-0 mt-0.5" />
                    <span className="text-sm text-red-800">{error}</span>
                </div>
            )}

            <div className="border-b border-default" data-testid="assurance-subtabs">
                <nav className="flex gap-1 -mb-px" role="tablist" aria-label="Assurance">
                    {SUB_TABS.map((t) => (
                        <button
                            key={t.id}
                            type="button"
                            role="tab"
                            aria-selected={sub === t.id}
                            onClick={() => setSub(t.id)}
                            data-testid={`assurance-subtab-${t.id}`}
                            className={`px-3 py-1.5 text-sm border-b-2 ${
                                sub === t.id
                                    ? 'border-primary-600 text-primary-900 font-medium'
                                    : 'border-transparent text-secondary hover:text-default'
                            }`}
                        >
                            {t.label}
                        </button>
                    ))}
                </nav>
            </div>

            {/* Where the evidence has got to — the loop, not the stage. */}
            {sub === 'conclusion' && report && !error && (
                <AssuranceLifecycleDiagram
                    onOpenInbox={setActiveTab ? () => setActiveTab('inbox') : undefined}
                    facts={{
                        // A shared service is evidenced by the calls it served — the verdict counts both.
                        totalRuns:
                            Number(
                                ((report.doing_its_job as Record<string, unknown> | undefined)?.runs as
                                    | Record<string, unknown>
                                    | undefined)?.total_runs ?? 0,
                            ) + (service?.served_calls ?? 0),
                        baselinesReady: Number(baselines?.ready ?? 0),
                        baselinesLearning: Number(baselines?.learning ?? 0),
                        healthState: health?.health_state ?? null,
                        verdictStatus: verdict?.status ?? null,
                        findingsOpen: (report.human_review as Record<string, unknown> | undefined)?.open as number | undefined,
                        findingsRaised: (report.human_review as Record<string, unknown> | undefined)?.raised as number | undefined,
                        reviewAvailable: (report.human_review as Record<string, unknown> | undefined)?.available as boolean | undefined,
                        snapshots: history?.total ?? 0,
                        lastSnapshotAt:
                            ((history?.reports?.[0] as { captured_at?: string | null } | undefined)?.captured_at ??
                                (history?.reports?.[0] as { created_at?: string | null } | undefined)?.created_at) ??
                            null,
                    }}
                />
            )}

            {/* The first twenty seconds (spec 1.2): can I rely on this
                system right now, and why? Everything below substantiates
                this — it never knows more than the sections can prove. */}
            {sub === 'conclusion' && summary && !error && (
                <div
                    className="bg-surface border border-default rounded-lg p-4 space-y-3"
                    data-testid="assurance-conclusion"
                >
                    <div className="flex items-start justify-between gap-4">
                        <div className="flex items-center gap-3">
                            <p className="text-sm font-medium text-default">
                                Assurance status
                            </p>
                            {summary.status && (
                                <span
                                    className={`inline-flex px-2.5 py-1 rounded text-sm font-semibold uppercase tracking-wide ${
                                        STATUS_CLASSES[summary.status.code] ??
                                        'bg-slate-100 text-slate-700'
                                    }`}
                                    data-testid="assurance-status"
                                >
                                    {summary.status.label}
                                </span>
                            )}
                        </div>
                        {/* The number measures how much EVIDENCE exists, not
                            how trustworthy the system is — labelled so a low
                            number cannot read as "basically not assured". */}
                        {/* Word only up here — "CONDITIONALLY ASSURED" next
                            to a numeral 0 reads as "so it isn't really
                            assured". The numeric ladder lives in the detail
                            block below. */}
                        <span
                            className="shrink-0 inline-flex px-2 py-0.5 rounded text-xs font-medium bg-primary-50 text-primary-700 border border-primary-200"
                            data-testid="assurance-level"
                            title={summary.level.means}
                        >
                            {`${summary.level.scale ?? 'Evidence maturity'}: ${summary.level.label}`}
                        </span>
                    </div>
                    {summary.status?.basis_note && (
                        <p className="text-xs text-secondary mt-1" data-testid="assurance-status-basis">
                            {summary.status.basis_note}
                        </p>
                    )}
                    {summary.what_we_know ? (
                        <div className="space-y-2 text-sm" data-testid="assurance-exec-rows">
                            <p>
                                <span className="font-medium text-default">What we know: </span>
                                <span className="text-default">{summary.what_we_know}</span>
                            </p>
                            {(summary.what_prevents_full_assurance?.length ?? 0) > 0 && (
                                <div data-testid="assurance-blockers">
                                    <span className="font-medium text-default">
                                        What prevents full assurance:
                                    </span>
                                    <ul className="list-disc pl-5 mt-1 text-default">
                                        {summary.what_prevents_full_assurance!.map((b, i) => (
                                            <li key={i}>{b}</li>
                                        ))}
                                    </ul>
                                </div>
                            )}
                            {summary.recommended_action && (
                                <p>
                                    <span className="font-medium text-default">
                                        Recommended action:{' '}
                                    </span>
                                    <span className="text-default">{summary.recommended_action}</span>
                                </p>
                            )}
                            {summary.why_it_matters && (
                                <p data-testid="assurance-why">
                                    <span className="font-medium text-default">
                                        Why this matters:{' '}
                                    </span>
                                    <span className="text-secondary">{summary.why_it_matters}</span>
                                </p>
                            )}
                        </div>
                    ) : (
                        /* 1.2 snapshots carry only the paragraph. */
                        <p className="text-sm text-default leading-relaxed">
                            {summary.conclusion}
                        </p>
                    )}
                    <div
                        className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-2"
                        data-testid="assurance-dimensions"
                    >
                        {Object.entries(summary.dimensions).map(([key, dim]) => {
                            const style = DIMENSION_STATUS[dim.status] ?? DIMENSION_STATUS.missing;
                            return (
                                <div
                                    key={key}
                                    className="border border-default rounded-lg p-2"
                                    data-testid={`dimension-${key}`}
                                    title={dim.detail}
                                >
                                    <span className={`inline-flex w-5 h-5 items-center justify-center rounded text-xs font-semibold ${style.cls}`}>
                                        {style.glyph}
                                    </span>
                                    <p className="text-[11px] font-medium text-default mt-1">
                                        {DIMENSION_LABELS[key] ?? key}
                                    </p>
                                    <p className="text-[11px] text-muted leading-snug mt-0.5">
                                        {dim.detail}
                                    </p>
                                </div>
                            );
                        })}
                    </div>
                </div>
            )}

            {sub === 'conclusion' && verdict && !error && (
                <div
                    className="bg-surface border border-default rounded-lg p-4 space-y-2"
                    data-testid="verdict-banner"
                >
                    <span
                        className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${
                            VERDICT_CLASSES[verdict.status] ??
                            'bg-slate-100 text-slate-700'
                        }`}
                        data-testid="verdict-status"
                    >
                        {VERDICT_LABELS[verdict.status] ?? verdict.status}
                    </span>
                    {/* A verdict earned only against observed history must
                        never borrow the authority of a signature. */}
                    {conditional && (
                        <span
                            className="ml-2 inline-flex px-2 py-0.5 rounded text-xs font-medium bg-slate-100 text-slate-600"
                            data-testid="verdict-conditional"
                        >
                            conditional — measured against observed history
                        </span>
                    )}
                    <p className="text-sm text-default" data-testid="verdict-conclusion">
                        {verdict.conclusion}
                    </p>
                    {failingDependencies.length > 0 && (
                        <div className="text-xs text-amber-900" data-testid="verdict-failing-dependencies">
                            <span className="font-medium">Critical dependencies not healthy: </span>
                            {failingDependencies.map((name, i) => {
                                const entry = dependencyHealth.find((d) => d.name === name);
                                const to = entry ? dependencyLink(entry) : null;
                                return (
                                    <span key={name}>
                                        {i > 0 && ', '}
                                        {to && setActiveTab ? (
                                            <button
                                                type="button"
                                                className="underline hover:no-underline"
                                                onClick={() => setActiveTab(to)}
                                                data-link={to}
                                            >
                                                {name}
                                            </button>
                                        ) : (
                                            name
                                        )}
                                    </span>
                                );
                            })}
                        </div>
                    )}
                </div>
            )}

            {sub === 'evidence' && health && !error && (
                <HealthBlock health={health} servedCalls={service?.served_calls ?? null} open={setActiveTab} />
            )}

            {sub === 'evidence' && service && !error && <ServiceBlock service={service} />}

            {sub === 'evidence' && dependencyHealth.length > 0 && !error && (
                <div className="bg-surface border border-default rounded-lg p-4" data-testid="report-dependency-health">
                    <DependencyHealthList entries={dependencyHealth} open={setActiveTab} />
                </div>
            )}

            {sub === 'evidence' && composition && !error && <CompositionReportSection section={composition} />}

            {sub === 'conclusion' && compliance && !error && (
                <div
                    className="bg-surface border border-default rounded-lg p-4 space-y-2"
                    data-testid="contract-compliance"
                >
                    <p className="text-sm font-medium text-default">
                        Contract compliance
                        {!compliance.under_signature && (
                            <span className="ml-2 inline-flex px-2 py-0.5 rounded text-xs font-medium bg-slate-100 text-slate-600">
                                no envelope under signature
                            </span>
                        )}
                    </p>
                    {compliance.unsigned_declaration && (
                        <p
                            className="text-xs text-amber-800"
                            data-testid="unsigned-declaration"
                        >
                            {compliance.unsigned_declaration}
                        </p>
                    )}
                    {compliance.envelope && (
                        <ul className="space-y-1" data-testid="compliance-terms">
                            {compliance.envelope.terms.map((t) => (
                                <li
                                    key={t.term}
                                    className="flex items-start justify-between gap-3 text-xs"
                                    data-testid={`term-${t.term}`}
                                >
                                    <span className="text-default">
                                        {t.label}
                                        {t.detail && (
                                            <span className="text-muted"> — {t.detail}</span>
                                        )}
                                    </span>
                                    <span
                                        className={`shrink-0 inline-flex px-2 py-0.5 rounded font-medium ${
                                            t.status === 'met'
                                                ? 'bg-green-100 text-green-800'
                                                : t.status === 'breached'
                                                  ? 'bg-red-100 text-red-800'
                                                  : 'bg-slate-100 text-slate-600'
                                        }`}
                                    >
                                        {envelopeTermStatusLabel(t.status)}
                                    </span>
                                </li>
                            ))}
                        </ul>
                    )}
                </div>
            )}

            {/* The numeric ladder — detail, deliberately away from the
                executive conclusion (the number measures evidence volume,
                and up there it competes with the status word). */}
            {sub === 'evidence' && summary && !error && (
                <div
                    className="bg-surface border border-default rounded-lg p-4"
                    data-testid="maturity-detail"
                >
                    <p className="text-sm font-medium text-default mb-1">
                        {summary.level.scale ?? 'Evidence maturity'}
                    </p>
                    <p className="text-sm text-default">
                        {summary.level.level >= 0
                            ? `${summary.level.level} — ${summary.level.label}. `
                            : `${summary.level.label}. `}
                        <span className="text-muted">{summary.level.means}</span>
                    </p>
                    {summary.level.next && (
                        <p className="text-xs text-muted mt-1" data-testid="assurance-next-level">
                            Next: Level {summary.level.next.level} —{' '}
                            {summary.level.next.label}. {summary.level.next.means}
                        </p>
                    )}

                    {/* The ladder as a checklist rather than a number. A
                        rung held above a broken one is still shown held —
                        cumulative scoring means it does not raise the level,
                        but hiding it makes operators redo work they have
                        already done. */}
                    {summary.level.rungs && summary.level.rungs.length > 0 && (
                        <ul className="mt-3 space-y-1" data-testid="assurance-rungs">
                            {summary.level.rungs.map((r) => (
                                <li
                                    key={r.level}
                                    className="flex items-start gap-2 text-xs"
                                    data-testid={`rung-${r.level}`}
                                >
                                    <span
                                        aria-hidden
                                        className={
                                            r.held ? 'text-green-600' : 'text-muted'
                                        }
                                    >
                                        {r.held ? '✓' : '○'}
                                    </span>
                                    <span
                                        className={
                                            r.held ? 'text-default' : 'text-muted'
                                        }
                                    >
                                        <span className="font-medium">
                                            L{r.level} {r.label}
                                        </span>{' '}
                                        — {r.requires}
                                    </span>
                                </li>
                            ))}
                        </ul>
                    )}

                    {/* The actionable half: what is blocking the next rung,
                        and the one thing to do about it. Without this the
                        ladder is a score an operator cannot move. */}
                    {summary.level.next?.action && (
                        <div
                            className="mt-3 rounded-md border border-default bg-surface-alt p-3"
                            data-testid="assurance-next-step"
                        >
                            <div className="text-xs font-semibold text-secondary">
                                To reach L{summary.level.next.level}
                            </div>
                            {summary.level.next.blocker && (
                                <p className="mt-1 text-xs text-muted">
                                    <span className="font-medium text-default">
                                        Blocking now:
                                    </span>{' '}
                                    {summary.level.next.blocker}
                                </p>
                            )}
                            <p className="mt-1 text-xs text-muted">
                                <span className="font-medium text-default">Do this:</span>{' '}
                                {summary.level.next.action}
                            </p>
                        </div>
                    )}
                </div>
            )}

            {sub === 'evidence' && baselines && !error && (
                <div
                    className="bg-surface border border-default rounded-lg p-4 space-y-2"
                    data-testid="baselines-block"
                >
                    <div className="flex items-start gap-2">
                        <p className="text-sm font-medium text-default">
                            Learned baselines
                        </p>
                        <HelpTip text="What normal looks like for this system, learned from its own run ledger. Every drift finding is a comparison against these rows; a baseline still learning never alerts." />
                        <span className="ml-auto text-xs text-muted" data-testid="baselines-counts">
                            {baselines.ready} ready
                            {baselines.learning > 0 && ` · ${baselines.learning} learning`}
                        </span>
                    </div>
                    {baselines.metrics.length === 0 ? (
                        <p className="text-xs text-muted" data-testid="baselines-empty">
                            No baselines yet — they build from the run ledger as
                            governed traffic arrives.
                        </p>
                    ) : (
                        <ul className="space-y-1">
                            {baselines.metrics.map((row) => (
                                <li
                                    key={`${row.basis ?? 'runs'}-${row.metric_key}`}
                                    className="flex items-center justify-between gap-3 text-xs"
                                    data-testid={
                                        row.basis === 'segments'
                                            ? `baseline-segments-${row.metric_key}`
                                            : `baseline-${row.metric_key}`
                                    }
                                    data-basis={row.basis ?? 'runs'}
                                >
                                    <span className="text-default">
                                        {driftMetricLabel(row.metric_key, row.basis)}
                                        <span className="text-muted">
                                            {' '}— {row.distribution
                                                ? distributionSummary(row.distribution)
                                                : baselineValue(row)}
                                        </span>
                                        {/* A resolution reset this yardstick. */}
                                        <BaselineEpochNote
                                            epochs={exclusions?.baseline_epochs ?? []}
                                            metricKey={row.metric_key}
                                            basis={row.basis}
                                        />
                                    </span>
                                    <span className="shrink-0 inline-flex items-center gap-2">
                                        <span className="text-muted">
                                            {row.sample_count}{' '}
                                            {row.basis === 'segments'
                                                ? SIGNAL_BASIS_LABELS.segments
                                                : 'runs'}
                                        </span>
                                        <span
                                            className={`inline-flex px-2 py-0.5 rounded font-medium ${
                                                row.state === 'ready'
                                                    ? 'bg-green-100 text-green-800'
                                                    : 'bg-blue-100 text-blue-800'
                                            }`}
                                        >
                                            {row.state}
                                        </span>
                                    </span>
                                </li>
                            ))}
                        </ul>
                    )}
                    {baselines.sub_baselines &&
                        (baselines.sub_baselines.seasonal > 0 ||
                            baselines.sub_baselines.per_actor > 0 ||
                            (baselines.sub_baselines.per_caller ?? 0) > 0) && (
                        <p className="text-xs text-muted" data-testid="sub-baselines">
                            Plus {baselines.sub_baselines.seasonal} seasonal and{' '}
                            {baselines.sub_baselines.per_actor} per-actor
                            sub-baselines sharpening detection.
                            {(baselines.sub_baselines.per_caller ?? 0) > 0 && (
                                <span data-testid="sub-baselines-per-caller">
                                    {' '}As a shared service, {baselines.sub_baselines.per_caller}{' '}
                                    per-caller sub-baseline(s) track each caller that drives a
                                    material share of its served calls.
                                </span>
                            )}
                        </p>
                    )}
                </div>
            )}

            {sub === 'findings' && recommendations.length > 0 && !error && (
                <div
                    className="bg-surface border border-default rounded-lg p-4 space-y-2"
                    data-testid="recommended-controls"
                >
                    <div className="flex items-start gap-2">
                        <p className="text-sm font-medium text-default">
                            Recommended controls
                        </p>
                        <HelpTip text="This report's findings turned into named next controls — derived by fixed rules from the sections above, advisory, never auto-applied." />
                    </div>
                    <ul className="space-y-1.5">
                        {recommendations.map((rec, i) => (
                            <li
                                key={`${rec.control}-${rec.subject ?? i}`}
                                className="flex items-start gap-2 text-xs"
                                data-testid={`rec-${rec.control}`}
                            >
                                <span
                                    className={`shrink-0 inline-flex px-2 py-0.5 rounded font-medium ${REC_SEVERITY_CLASSES[rec.severity]}`}
                                >
                                    {rec.severity}
                                </span>
                                <span className="text-default">
                                    {CONTROL_LABELS[rec.control] ?? rec.control}
                                    {rec.subject && (
                                        <span className="font-mono"> {rec.subject}</span>
                                    )}
                                    <span className="text-muted"> — {rec.reason}</span>
                                </span>
                            </li>
                        ))}
                    </ul>
                </div>
            )}

            {sub === 'findings' && exceptions && !error && (
                <div
                    className="bg-surface border border-default rounded-lg p-4"
                    data-testid="exceptions-block"
                >
                    <p className="text-sm font-medium text-default mb-2">
                        Exceptions in the period
                    </p>
                    <div className="flex flex-wrap gap-3 text-xs text-muted">
                        <span>Guardrail blocks <span className="text-default">{exceptions.guardrail_blocks ?? 0}</span></span>
                        <span>Policy denials <span className="text-default">{exceptions.policy_denials ?? 0}</span></span>
                        <span>Approval requests <span className="text-default">{exceptions.approval_requests ?? 0}</span></span>
                        <span>Automatic responses <span className="text-default">{exceptions.automatic_responses ?? 0}</span></span>
                    </div>
                </div>
            )}

            {sub === 'findings' && changes && !error && (
                <div
                    className="bg-surface border border-default rounded-lg p-4"
                    data-testid="changes-block"
                >
                    <p className="text-sm font-medium text-default mb-2">
                        Changes in the period
                    </p>
                    <div className="flex flex-wrap gap-3 text-xs text-muted">
                        <span>
                            Contract versions minted{' '}
                            <span className="text-default">
                                {(changes.contract_versions_minted as number[] | undefined)?.length ?? 0}
                            </span>
                        </span>
                        <span>
                            Attributed model changes{' '}
                            <span className="text-default">
                                {(changes.attributed_model_changes as number) ?? 0}
                            </span>
                        </span>
                        <span>
                            Attributed dependency changes{' '}
                            <span className="text-default">
                                {(changes.attributed_dependency_changes as number) ?? 0}
                            </span>
                        </span>
                        {Boolean(changes.config_drifted_now) && (
                            <span className="text-amber-800">
                                config currently drifted from the active contract
                            </span>
                        )}
                    </div>
                </div>
            )}

            {sub === 'evidence' && !error && (
                <JudgementExclusionsSection exclusions={exclusions} excludedPeriod={excludedPeriod} />
            )}

            {sub === 'evidence' && notCovered.length > 0 && !error && (
                <div
                    className="bg-surface border border-default rounded-lg p-4"
                    data-testid="not-covered"
                >
                    <div className="flex items-start gap-2">
                        <ShieldCheck className="w-4 h-4 text-muted shrink-0 mt-0.5" />
                        <div>
                            <p className="text-sm font-medium text-default">
                                What this report does not cover
                            </p>
                            <ul className="mt-2 space-y-1">
                                {notCovered.map((gap, i) => (
                                    <li key={i} className="text-xs text-muted">
                                        {gap}
                                    </li>
                                ))}
                            </ul>
                        </div>
                    </div>
                </div>
            )}

            {sub === 'snapshots' && !error && (!history || history.total === 0) && (
                <p className="text-sm text-muted" data-testid="assurance-history-empty">
                    No snapshot captured yet. A snapshot freezes this report as a signed
                    certificate an auditor can hold; capture one at each sign-off.
                </p>
            )}

            {sub === 'snapshots' && history && history.total > 0 && (
                <div data-testid="assurance-history">
                    <p className="text-xs font-medium text-default mb-2">
                        Captured snapshots
                    </p>
                    <ul className="space-y-1">
                        {history.reports.map((r) => (
                            <li
                                key={r.id}
                                className="flex items-center justify-between text-xs text-muted border border-default rounded px-3 py-2"
                                data-testid={`snapshot-${r.id}`}
                            >
                                <span>
                                    {r.created_at
                                        ? new Date(r.created_at).toLocaleString()
                                        : 'unknown time'}
                                    {' · '}
                                    {r.trigger}
                                    {r.captured_by ? ` by ${r.captured_by}` : ''}
                                </span>
                                <span className="flex items-center gap-2">
                                    {r.verdict_status && (
                                        <span
                                            className={`inline-flex px-1.5 py-0.5 rounded font-medium ${
                                                VERDICT_CLASSES[r.verdict_status] ??
                                                'bg-slate-100 text-slate-600'
                                            }`}
                                        >
                                            {VERDICT_LABELS[r.verdict_status] ??
                                                r.verdict_status}
                                        </span>
                                    )}
                                    {r.signed && (
                                        <span
                                            className="inline-flex px-1.5 py-0.5 rounded font-medium bg-sky-100 text-sky-800"
                                            title="Attested with the deployment audit key at capture time"
                                            data-testid={`signed-${r.id}`}
                                        >
                                            signed
                                        </span>
                                    )}
                                    <span className="font-mono">
                                        {r.contract_version
                                            ? `contract v${r.contract_version}`
                                            : 'no contract'}
                                    </span>
                                    <button
                                        type="button"
                                        disabled={busy === `cert-${r.id}`}
                                        onClick={() => void downloadCertificate(r.id)}
                                        title="Download as a portable Assurance Certificate (JSON)"
                                        className="p-1 text-muted hover:text-default disabled:opacity-50"
                                        data-testid={`cert-${r.id}`}
                                    >
                                        <Download className="w-3.5 h-3.5" />
                                    </button>
                                    <button
                                        type="button"
                                        disabled={busy === `cert-${r.id}`}
                                        onClick={() => void downloadCertificate(r.id, true)}
                                        title="Print / save as PDF"
                                        className="p-1 text-muted hover:text-default disabled:opacity-50"
                                        data-testid={`cert-pdf-${r.id}`}
                                    >
                                        <Printer className="w-3.5 h-3.5" />
                                    </button>
                                </span>
                            </li>
                        ))}
                    </ul>
                </div>
            )}
        </div>
    );
}

/** What a shared service did for its callers. */
/** "re-learning since … — accepted change by …" under a
 *  report baseline an epoch reset (the aggregate row: caller 'all'). */
function BaselineEpochNote({
    epochs,
    metricKey,
    basis,
}: {
    epochs: BaselineEpoch[];
    metricKey: string;
    basis?: SignalBasis;
}) {
    const epoch = epochInForce(epochs, metricKey, basis);
    if (!epoch) return null;
    return (
        <span className="block text-secondary" data-testid={`baseline-epoch-${metricKey}`}>
            {epochText(epoch)}
        </span>
    );
}

function ServiceBlock({ service }: { service: ServiceFacts }) {
    const outcomes = Object.entries(service.outcome_mix)
        .filter(([, n]) => n > 0)
        .map(([k, n]) => `${n} ${k.replace(/_/g, ' ')}`)
        .join(' · ');
    const pct = (v: number | null) => (v === null ? '—' : `${Math.round(v * 100)}%`);
    return (
        <div className="bg-surface border border-default rounded-lg p-4 space-y-2" data-testid="service-facts">
            <div className="flex items-start gap-2">
                <p className="text-sm font-medium text-default">Served as a shared service</p>
                <HelpTip text="This system roots no runs of its own; it is called inside other systems' runs. Every signal in this report is computed from those served calls (run segments): answered share, tool errors, cost per answered call." />
            </div>
            <div className="flex flex-wrap gap-3 text-xs text-muted">
                <span>Served calls <span className="text-default" data-testid="service-served-calls">{service.served_calls}</span></span>
                <span>Answered <span className="text-default">{pct(service.success_rate)}</span></span>
                <span>Tool error rate <span className="text-default">{pct(service.tool_error_rate)}</span></span>
                <span>
                    Cost per answered call{' '}
                    <span className="text-default">
                        {service.cost_per_answered_call === null
                            ? '—'
                            : `$${service.cost_per_answered_call.toFixed(4)}`}
                    </span>
                </span>
            </div>
            {outcomes && (
                <p className="text-xs text-muted" data-testid="service-outcomes">
                    Outcomes: {outcomes}
                </p>
            )}
        </div>
    );
}

function HealthBlock({
    health,
    servedCalls,
    open,
}: {
    health: SystemHealth;
    servedCalls: number | null;
    open?: (tab: string) => void;
}) {
    const state = health.health_state;

    return (
        <div className="bg-surface border border-default rounded-lg p-4 space-y-3" data-testid="health-block">
            <div className="flex items-center gap-3">
                <span
                    className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${
                        HEALTH_CLASSES[state] ?? 'bg-slate-100 text-slate-700'
                    }`}
                    data-testid="health-state"
                >
                    {HEALTH_LABELS[state] ?? state}
                </span>
                <span className="text-2xl font-semibold text-default" data-testid="health-score">
                    {health.health_score === null || health.health_score === undefined
                        ? '—'
                        : `${Math.round(health.health_score * 100)}%`}
                </span>
                {health.chain_integrity_pct !== null &&
                    health.chain_integrity_pct !== undefined && (
                        <span
                            className={`text-xs ${
                                health.is_trustworthy ? 'text-muted' : 'text-amber-800'
                            }`}
                            data-testid="chain-integrity"
                        >
                            {Math.round(health.chain_integrity_pct * 100)}% of runs observed
                            intact
                        </span>
                    )}
            </div>

            {health.dependency_cap && state === 'degraded' && (
                <DependencyCapBanner cap={health.dependency_cap} open={open} />
            )}

            {health.reasons.map((reason, i) => (
                <p key={i} className="text-sm text-muted" data-testid="health-reason">
                    {reason}
                </p>
            ))}

            <SignalBasisNote basis={health.signal_basis} servedCalls={servedCalls} />

            {/* Always expandable: a score nobody can decompose is a score
                nobody trusts. */}
            <div className="flex flex-wrap gap-3" data-testid="health-components">
                {HEALTH_COMPONENTS.map(({ key, label }) => {
                    const value = health[key] as number | null | undefined;
                    // Kind-aware profile: `signal_levels` says whether this
                    // signal counts toward the verdict for this shape of
                    // system. `not_applicable` renders as "n/a" — never as a
                    // zero and never as a pass, because a signal we did not
                    // ask about has not been satisfied.
                    const signal = key.replace(/_score$/, '');
                    const level = health.signal_levels?.[signal]?.level;
                    const na = level === 'not_applicable';
                    const advisory = level === 'advisory';
                    return (
                        <span
                            key={key}
                            className="text-xs text-muted"
                            data-testid={`component-${key}`}
                            title={
                                na
                                    ? notApplicableText(signal, health)
                                    : advisory
                                      ? 'Shown but not counted toward the health verdict'
                                      : undefined
                            }
                        >
                            {label}{' '}
                            <span className="text-default">
                                {na
                                    ? 'n/a'
                                    : value === null || value === undefined
                                      ? 'not measured'
                                      : `${Math.round(value * 100)}%`}
                            </span>
                            {advisory && (
                                <span
                                    className="ml-1 rounded bg-surface-alt px-1 py-0.5 text-[10px] uppercase tracking-wide text-muted"
                                    data-testid={`component-${key}-advisory`}
                                >
                                    advisory
                                </span>
                            )}
                        </span>
                    );
                })}
            </div>

            {/* The disclosure. If choosing a kind could quietly stop a signal
                from counting, `system_kind` would be a dropdown for making a
                bad health score disappear — so every relaxation is stated. */}
            {(health.profile_relaxations?.length ?? 0) > 0 && (
                <div
                    className="rounded-lg border border-default bg-surface-alt p-3"
                    data-testid="profile-relaxations"
                >
                    <div className="text-xs font-semibold text-secondary">
                        Scored as a {health.system_kind}, which relaxes{' '}
                        {health.profile_relaxations!.length} of 6 signals
                    </div>
                    <ul className="mt-1 list-disc space-y-0.5 pl-4 text-xs text-muted">
                        {health.profile_relaxations!.map((r) => (
                            <li key={r.signal}>{r.reason}</li>
                        ))}
                    </ul>
                </div>
            )}
        </div>
    );
}

export default AssurancePanel;

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Compliance → Human Oversight.
 *
 * `GET /compliance/oversight` per system: who exercises oversight (a
 * DECLARED fact from the profile), whether an override is available
 * (run:abort held — recomputed), approvals requested / decided by a human
 * / lapsed / in flight (recomputed, `n of m`), and movement in approval
 * latency and denial rate against the system's own baseline — an
 * INDICATOR: shown as movement, never as a threshold, never red or green.
 * The approvals n-of-m is a rate, so holds raised inside a
 * declared incident window are not judged. The tiles stay the complete
 * record and the excluded count is stated beside them — never hidden.
 * Below, the blind review queue and human agreement per judged check,
 * each linking to the check's queue on the Inbox page.
 */

import { useCallback, useMemo, useState } from 'react';
import { ArrowRight, Scale, UserCheck } from 'lucide-react';

import { useGateway } from '../../../hooks/useGateway';
import { useKeyedLoad } from '../../../hooks/useKeyedLoad';
import { getBoolean, getRecord, getString } from '../../../lib/narrow';
import type { OversightSummary } from '../../../lib/complianceEvidenceTypes';
import { TABLE_CONTAINER, TABLE_TBODY, TABLE_TH, TABLE_THEAD } from '../../ui/tableStyles';
import { HelpTip } from '../../ui/HelpTip';
import RecordDialog from '../RecordDialog';
import { BasisChip, ErrorNote, Loading, RecordLink, Section, StatusChip, WindowPicker } from '../ComplianceUi';
import {
    agreementText,
    formatDay,
    INPUT_CLASS,
    LINK_BUTTON,
    movementText,
    nOfM,
    readAgreement,
    readOversightStatement,
    reviewQueueText,
    windowFor,
    type OversightStatement,
    type WindowPreset,
} from '../complianceFormat';
import type { CompliancePageProps } from '../complianceSections';

const APPROVAL_KEYS: { key: string; label: string; help: string }[] = [
    { key: 'requested', label: 'Requested', help: 'Approval gates that opened in the window (awaiting_human records).' },
    { key: 'decided_by_human', label: 'Decided by a human', help: 'Gates a person resolved — allowed or denied — with a sealed resolved record.' },
    { key: 'lapsed', label: 'Lapsed', help: 'Gates that expired without a decision (expired records).' },
    { key: 'in_flight', label: 'In flight', help: 'Gates still open at the end of the window.' },
];

/** Holds raised inside declared incident windows. */
const EXCLUDED_KEY = 'excluded_in_incident_windows';

export default function OversightPage({ setActiveTab }: CompliancePageProps) {
    const { gatewayClient, fetchAISystems } = useGateway();
    const [preset, setPreset] = useState<WindowPreset>('30d');
    const [systemId, setSystemId] = useState('');
    const [openRecord, setOpenRecord] = useState<string | null>(null);

    const systemsLoader = useCallback(() => fetchAISystems({ limit: 200 }), [fetchAISystems]);
    const systems = useKeyedLoad('systems', systemsLoader).data?.systems ?? [];

    // Fixed until the preset changes. Recomputing it on every render would move
    // the window (and the load key) each time the clock crosses a minute, so
    // any re-render would reload the page and drop what the operator has open.
    const window = useMemo(() => windowFor(preset), [preset]);
    const loader = useCallback(
        (): Promise<OversightSummary> =>
            gatewayClient.getComplianceOversight({ ai_system_id: systemId || undefined, from: window.from, to: window.to }),
        [gatewayClient, systemId, window.from, window.to],
    );
    const { data, error, loading } = useKeyedLoad(`${systemId}:${window.from}:${window.to}`, loader, 'Failed to load the oversight summary');

    if (loading && !data) return <Loading label="Loading oversight…" testId="oversight-loading" />;
    if (error) return <ErrorNote message={error} testId="oversight-error" />;
    if (!data) return null;

    // Holds raised inside declared incident windows: recorded, not judged.
    const excludedHolds = data.approvals[EXCLUDED_KEY] ?? 0;

    const perSystem = data.systems.map((raw) => ({
        id: getString(raw, 'ai_system_id') ?? '',
        name: getString(raw, 'name') ?? getString(raw, 'ai_system_id') ?? '—',
        assignment: getString(raw, 'oversight_assignment') ?? null,
        assignmentBasis: getString(raw, 'assignment_basis') ?? 'declared',
        override: readOversightStatement(getRecord(raw, 'override_available')),
        approvals: readOversightStatement(getRecord(raw, 'approvals')),
        movement: readOversightStatement(getRecord(raw, 'movement')),
    }));
    const checks = data.judged_checks.map((raw) => ({
        id: getString(raw, 'check_id') ?? '',
        name: getString(raw, 'name') ?? getString(raw, 'check_id') ?? '—',
        template: getString(raw, 'template') ?? null,
        enabled: getBoolean(raw, 'enabled') ?? false,
        queue: getRecord(raw, 'review_queue') ?? null,
        agreement: readAgreement(getRecord(raw, 'agreement')),
    }));

    return (
        <div className="space-y-4" data-testid="compliance-oversight">
            <div className="flex items-center justify-between gap-3 flex-wrap">
                <p className="text-sm text-muted">
                    Window {formatDay(data.window_start)} → {formatDay(data.window_end)}. Assignment is declared; override
                    availability and approvals are recomputed from sealed records; movement is an indicator.
                </p>
                <div className="flex items-center gap-3 flex-wrap">
                    <label className="inline-flex items-center gap-2 text-xs">
                        <span className="text-muted uppercase tracking-wide">System</span>
                        <select value={systemId} onChange={(e) => setSystemId(e.target.value)} className={INPUT_CLASS} data-testid="oversight-system">
                            <option value="">all systems</option>
                            {systems.map((s) => (
                                <option key={s.id} value={s.id}>
                                    {s.display_name || s.name}
                                </option>
                            ))}
                        </select>
                    </label>
                    <WindowPicker value={preset} onChange={setPreset} testId="oversight-window" />
                </div>
            </div>

            <div className="grid grid-cols-2 xl:grid-cols-4 gap-3" data-testid="oversight-approvals">
                {APPROVAL_KEYS.map((k) => (
                    <div key={k.key} className="rounded-lg border border-default bg-surface p-3" data-testid={`approvals-${k.key}`}>
                        <div className="flex items-center gap-1 text-xs font-semibold text-muted uppercase tracking-wide">
                            {k.label} <HelpTip text={k.help} />
                        </div>
                        <p className="text-2xl font-semibold text-default">{(data.approvals[k.key] ?? 0).toLocaleString()}</p>
                    </div>
                ))}
            </div>
            {excludedHolds > 0 && (
                <p className="text-xs text-amber-900" data-testid="approvals-excluded">
                    {excludedHolds.toLocaleString()} inside incident windows, not judged — counted above as
                    recorded; the per-system approvals rate below does not grade them. A window only excuses what it
                    covers: holds raised outside every window that lapsed still count.
                </p>
            )}

            <Section
                title={
                    <span className="inline-flex items-center gap-1.5">
                        <UserCheck className="w-4 h-4 text-primary-600" /> Oversight per AI System
                    </span>
                }
                subtitle={data.movement_note}
                testId="oversight-systems"
            >
                {perSystem.length === 0 ? (
                    <p className="text-xs text-muted" data-testid="oversight-no-systems">
                        No active AI System in scope.
                    </p>
                ) : (
                    <div className={`${TABLE_CONTAINER} overflow-x-auto`}>
                        <table className="w-full text-xs">
                            <thead className={TABLE_THEAD}>
                                <tr>
                                    <th className={TABLE_TH}>AI System</th>
                                    <th className={TABLE_TH}>Oversight assignment</th>
                                    <th className={TABLE_TH}>Override available</th>
                                    <th className={TABLE_TH}>Approvals decided by a human</th>
                                    <th className={TABLE_TH}>Movement</th>
                                </tr>
                            </thead>
                            <tbody className={TABLE_TBODY}>
                                {perSystem.map((s) => (
                                    <tr key={s.id} data-testid={`oversight-row-${s.id}`}>
                                        <td className="px-4 py-2.5 align-top">
                                            {setActiveTab ? (
                                                <button type="button" className="font-medium text-primary-600 hover:underline text-left" onClick={() => setActiveTab(`ai-systems:${s.id}:compliance`)}>
                                                    {s.name}
                                                </button>
                                            ) : (
                                                <span className="font-medium text-default">{s.name}</span>
                                            )}
                                        </td>
                                        <td className="px-4 py-2.5 align-top" data-testid={`oversight-assignment-${s.id}`}>
                                            <div className="flex items-start gap-2">
                                                <BasisChip basis={s.assignmentBasis} />
                                                {s.assignment ? (
                                                    <span className="text-default">{s.assignment}</span>
                                                ) : (
                                                    <span className="text-amber-800">not declared — declare it on the compliance profile</span>
                                                )}
                                            </div>
                                        </td>
                                        <td className="px-4 py-2.5 align-top" data-testid={`oversight-override-${s.id}`}>
                                            <StatementCell statement={s.override} onOpenRecord={setOpenRecord} />
                                        </td>
                                        <td className="px-4 py-2.5 align-top" data-testid={`oversight-approvals-${s.id}`}>
                                            <StatementCell statement={s.approvals} onOpenRecord={setOpenRecord} />
                                        </td>
                                        <td className="px-4 py-2.5 align-top" data-testid={`oversight-movement-${s.id}`}>
                                            {s.movement ? (
                                                <div className="space-y-0.5">
                                                    <div className="flex items-center gap-1.5">
                                                        <BasisChip basis={s.movement.basis} />
                                                        <span className="text-secondary">{movementText(s.movement.facts) || s.movement.detail || 'no movement stated'}</span>
                                                    </div>
                                                    {movementText(s.movement.facts) && s.movement.detail && <p className="text-muted">{s.movement.detail}</p>}
                                                    {s.movement.limit && <p className="text-amber-800">Limit: {s.movement.limit}</p>}
                                                </div>
                                            ) : (
                                                <span className="text-muted">—</span>
                                            )}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </Section>

            <Section
                title={
                    <span className="inline-flex items-center gap-1.5">
                        <Scale className="w-4 h-4 text-primary-600" /> Blind review queue and human agreement
                    </span>
                }
                subtitle={`${data.review_queue.pending ?? 0} pending · ${data.review_queue.graded ?? 0} graded across every judged check. Agreement is stated per check for its current judge over 30 days; fewer than 10 grades is an insufficient sample.`}
                testId="oversight-checks"
            >
                {checks.length === 0 ? (
                    <p className="text-xs text-muted" data-testid="oversight-no-checks">
                        No judged (semantic) check is configured, so there is no queue and nothing to agree with.
                    </p>
                ) : (
                    <div className={`${TABLE_CONTAINER} overflow-x-auto`}>
                        <table className="w-full text-xs">
                            <thead className={TABLE_THEAD}>
                                <tr>
                                    <th className={TABLE_TH}>Check</th>
                                    <th className={TABLE_TH}>Template</th>
                                    <th className={TABLE_TH}>Review queue</th>
                                    <th className={TABLE_TH}>Human agreement</th>
                                    <th className={TABLE_TH}></th>
                                </tr>
                            </thead>
                            <tbody className={TABLE_TBODY}>
                                {checks.map((c) => (
                                    <tr key={c.id} data-testid={`oversight-check-${c.id}`}>
                                        <td className="px-4 py-2.5">
                                            <span className="font-medium text-default">{c.name}</span>
                                            {!c.enabled && <span className="ml-2 text-muted">disabled</span>}
                                        </td>
                                        <td className="px-4 py-2.5 font-mono text-secondary">{c.template ? `judge.${c.template}` : '—'}</td>
                                        <td className="px-4 py-2.5 text-secondary" data-testid={`oversight-queue-${c.id}`}>
                                            {reviewQueueText(c.queue)}
                                        </td>
                                        <td className="px-4 py-2.5" data-testid={`oversight-agreement-${c.id}`}>
                                            <span className="font-mono text-secondary">{agreementText(c.agreement)}</span>
                                            {c.agreement?.belowThreshold && !c.agreement.insufficient && (
                                                <span className="block text-amber-800">below threshold — judge uncalibrated</span>
                                            )}
                                        </td>
                                        <td className="px-4 py-2.5 text-right">
                                            {setActiveTab && (
                                                <button
                                                    type="button"
                                                    className={`${LINK_BUTTON} inline-flex items-center gap-1`}
                                                    onClick={() => setActiveTab(`inbox:reviews:${c.id}`)}
                                                    data-testid={`oversight-open-check-${c.id}`}
                                                >
                                                    Open queue <ArrowRight className="w-3 h-3" />
                                                </button>
                                            )}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </Section>

            {openRecord && <RecordDialog recordId={openRecord} onClose={() => setOpenRecord(null)} />}
        </div>
    );
}

function StatementCell({ statement, onOpenRecord }: { statement: OversightStatement | null; onOpenRecord: (id: string) => void }) {
    if (!statement) return <span className="text-muted">—</span>;
    return (
        <div className="space-y-0.5">
            <div className="flex items-center gap-1.5 flex-wrap">
                <BasisChip basis={statement.basis} />
                <StatusChip status={statement.status} />
                {statement.m !== null && <span className="text-default font-medium">{nOfM(statement.n, statement.m)}</span>}
            </div>
            {statement.detail && <p className="text-secondary">{statement.detail}</p>}
            {statement.limit && <p className="text-amber-800">Limit: {statement.limit}</p>}
            {statement.citations.length > 0 && (
                <p className="text-muted">
                    cites{' '}
                    {statement.citations.slice(0, 3).map((rid) => (
                        <span key={rid} className="mr-1.5">
                            <RecordLink recordId={rid} onOpen={onOpenRecord} />
                        </span>
                    ))}
                    {statement.citations.length > 3 ? `+${statement.citations.length - 3} more` : ''}
                </p>
            )}
        </div>
    );
}

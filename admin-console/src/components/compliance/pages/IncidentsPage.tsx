/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Compliance → Incidents: the register.
 *
 * Each incident carries the frameworks in scope, the per-framework
 * deadlines computed from their `deadline_rules` — or, for a staged regime
 * (DORA, NIS2), from each stage's own anchor: awareness, the recorded
 * classification time, or the previous stage's recorded submission — shown
 * with a status (open / due soon / overdue / reported on time / reported
 * late / waiting / closed, not submitted) and NEVER enforced (`deadlines_enforced: false` on every row) — the
 * authority and reference once reported, linked runs and inbox items, and
 * a sealed history: every state change is an event with its record id.
 * Every write returns a `seal` block; an unsealed change is shown as such.
 */

import { useCallback, useState } from 'react';
import { Plus, RefreshCw, Siren } from 'lucide-react';

import { useGateway } from '../../../hooks/useGateway';
import { useKeyedLoad } from '../../../hooks/useKeyedLoad';
import { errorMessage } from '../../../lib/narrow';
import {
    INCIDENT_STATUSES,
    type ComplianceIncident,
    type IncidentChangeResponse,
    type IncidentDeadline,
    type IncidentDetail as IncidentDetailShape,
    type SealStatus,
} from '../../../lib/complianceEvidenceTypes';
import { TABLE_CONTAINER, TABLE_ROW_HOVER, TABLE_TBODY, TABLE_TH, TABLE_THEAD } from '../../ui/tableStyles';
import IncidentForm from '../IncidentForm';
import { toIncidentCreateBody, toIncidentUpdateBody, toLocalInput, type IncidentFormValues } from '../complianceForms';
import RecordDialog from '../RecordDialog';
import { ErrorNote, Loading, RecordLink, SealNote, Section } from '../ComplianceUi';
import {
    BUTTON_PRIMARY,
    BUTTON_SECONDARY,
    DEADLINE_STATUS_CHIP,
    DEADLINES_NOT_ENFORCED,
    deadlineAnchorText,
    deadlineText,
    formatWhen,
    INCIDENT_SEVERITY_CHIP,
    INCIDENT_STATUS_CHIP,
    incidentEventText,
    INPUT_CLASS,
    leadDeadline,
    LINK_BUTTON,
} from '../complianceFormat';
import type { CompliancePageProps } from '../complianceSections';

type Panel = { kind: 'none' } | { kind: 'create' } | { kind: 'detail'; id: string };

export default function IncidentsPage({ framework, frameworks, setActiveTab }: CompliancePageProps) {
    const { gatewayClient, fetchAISystems } = useGateway();
    const [status, setStatus] = useState('');
    const [onlyFramework, setOnlyFramework] = useState(false);
    const [panel, setPanel] = useState<Panel>({ kind: 'none' });
    const [openRecord, setOpenRecord] = useState<string | null>(null);
    const [creating, setCreating] = useState(false);
    const [createError, setCreateError] = useState('');
    const [lastChange, setLastChange] = useState<IncidentChangeResponse | null>(null);

    const systemsLoader = useCallback(() => fetchAISystems({ limit: 200 }), [fetchAISystems]);
    const systems = (useKeyedLoad('systems', systemsLoader).data?.systems ?? []).map((s) => ({ id: s.id, name: s.display_name || s.name }));
    const systemName = (id: string | null | undefined) => (id ? (systems.find((s) => s.id === id)?.name ?? id) : '—');

    const loader = useCallback(
        () => gatewayClient.listComplianceIncidents({ status: status || undefined, framework: onlyFramework ? framework : undefined }),
        [gatewayClient, status, onlyFramework, framework],
    );
    const { data, error, loading, reload } = useKeyedLoad(`${status}:${onlyFramework ? framework : '*'}`, loader, 'Failed to load incidents');
    const items = data?.items ?? [];
    const enforced = items.some((i) => i.deadlines_enforced);

    const create = async (values: IncidentFormValues) => {
        setCreating(true);
        setCreateError('');
        try {
            const res = await gatewayClient.createComplianceIncident(toIncidentCreateBody(values));
            setLastChange(res);
            setPanel({ kind: 'detail', id: res.incident.id });
            reload();
        } catch (e: unknown) {
            setCreateError(errorMessage(e, 'The incident could not be opened'));
        } finally {
            setCreating(false);
        }
    };

    return (
        <div className="space-y-4" data-testid="compliance-incidents">
            <div className="flex items-center justify-between gap-3 flex-wrap">
                <p className="text-sm text-muted max-w-3xl">
                    Per-framework reporting deadlines are computed from each framework{'’'}s rules for the incident{'’'}s classification.{' '}
                    <strong className="text-default">{enforced ? 'Deadlines are enforced for some rows.' : DEADLINES_NOT_ENFORCED}</strong> Every change is
                    sealed as an incident record.
                </p>
                <div className="flex items-center gap-3 flex-wrap text-xs">
                    <label className="inline-flex items-center gap-2">
                        <span className="text-muted uppercase tracking-wide">Status</span>
                        <select value={status} onChange={(e) => setStatus(e.target.value)} className={INPUT_CLASS} data-testid="incidents-status">
                            <option value="">any</option>
                            {INCIDENT_STATUSES.map((s) => (
                                <option key={s} value={s}>
                                    {s}
                                </option>
                            ))}
                        </select>
                    </label>
                    <label className="inline-flex items-center gap-1.5">
                        <input type="checkbox" checked={onlyFramework} onChange={(e) => setOnlyFramework(e.target.checked)} data-testid="incidents-only-framework" />
                        only {frameworks.find((f) => f.id === framework)?.title ?? framework}
                    </label>
                    <button type="button" onClick={reload} disabled={loading} className={BUTTON_SECONDARY}>
                        <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} /> Refresh
                    </button>
                    <button type="button" onClick={() => setPanel({ kind: 'create' })} className={BUTTON_PRIMARY} data-testid="incidents-new">
                        <Plus className="w-3.5 h-3.5" /> Open incident
                    </button>
                </div>
            </div>

            {error && <ErrorNote message={error} testId="incidents-error" />}

            <div className={`${TABLE_CONTAINER} overflow-x-auto`}>
                <table className="w-full text-xs">
                    <thead className={TABLE_THEAD}>
                        <tr>
                            <th className={TABLE_TH}>Incident</th>
                            <th className={TABLE_TH}>Severity</th>
                            <th className={TABLE_TH}>Status</th>
                            <th className={TABLE_TH}>System</th>
                            <th className={TABLE_TH}>Frameworks</th>
                            <th className={TABLE_TH}>Became aware</th>
                            <th className={TABLE_TH}>Next deadline</th>
                            <th className={TABLE_TH}>Sealed</th>
                        </tr>
                    </thead>
                    <tbody className={TABLE_TBODY}>
                        {loading && items.length === 0 && (
                            <tr>
                                <td colSpan={8} className="px-4 py-6 text-center text-muted">
                                    Loading…
                                </td>
                            </tr>
                        )}
                        {!loading && items.length === 0 && !error && (
                            <tr>
                                <td colSpan={8} className="px-4 py-6 text-center text-muted" data-testid="incidents-empty">
                                    No incident on the register{status ? ` with status ${status}` : ''}.
                                </td>
                            </tr>
                        )}
                        {items.map((i) => {
                            const lead = leadDeadline(i.deadline_status);
                            return (
                                <tr
                                    key={i.id}
                                    className={`${TABLE_ROW_HOVER} cursor-pointer ${panel.kind === 'detail' && panel.id === i.id ? 'bg-surface-alt' : ''}`}
                                    onClick={() => setPanel({ kind: 'detail', id: i.id })}
                                    data-testid={`incident-row-${i.id}`}
                                >
                                    <td className="px-4 py-2">
                                        <div className="font-medium text-default">{i.title}</div>
                                        <div className="text-muted font-mono">{i.classification}</div>
                                    </td>
                                    <td className="px-4 py-2">
                                        <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${INCIDENT_SEVERITY_CHIP[i.severity] ?? 'bg-surface-raised text-secondary'}`}>{i.severity}</span>
                                    </td>
                                    <td className="px-4 py-2">
                                        <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${INCIDENT_STATUS_CHIP[i.status] ?? 'bg-surface-raised text-secondary'}`}>{i.status}</span>
                                    </td>
                                    <td className="px-4 py-2 text-secondary">{systemName(i.ai_system_id)}</td>
                                    <td className="px-4 py-2 text-secondary">{(i.frameworks ?? []).join(', ') || '—'}</td>
                                    <td className="px-4 py-2 text-secondary whitespace-nowrap">{formatWhen(i.became_aware_at)}</td>
                                    <td className="px-4 py-2" data-testid={`incident-deadline-${i.id}`}>
                                        {lead ? <DeadlineChip deadline={lead} /> : <span className="text-muted">no rule applies</span>}
                                    </td>
                                    <td className="px-4 py-2 text-secondary">{i.last_record_id ? 'sealed' : <span className="text-amber-800">not sealed</span>}</td>
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
            </div>

            {panel.kind === 'create' && (
                <Section title="Open an incident" subtitle="The frameworks in scope decide which deadline rules apply; the classification names the rule." testId="incident-create">
                    <IncidentForm initial={null} frameworks={frameworks} systems={systems} saving={creating} error={createError} onSubmit={(v) => void create(v)} onCancel={() => setPanel({ kind: 'none' })} />
                </Section>
            )}

            {panel.kind === 'detail' && (
                <IncidentDetail
                    key={panel.id}
                    incidentId={panel.id}
                    systems={systems}
                    systemName={systemName}
                    frameworks={frameworks}
                    initialChange={lastChange?.incident.id === panel.id ? lastChange : null}
                    onChanged={reload}
                    onClose={() => setPanel({ kind: 'none' })}
                    onOpenRecord={setOpenRecord}
                    setActiveTab={setActiveTab}
                />
            )}

            {openRecord && <RecordDialog recordId={openRecord} onClose={() => setOpenRecord(null)} />}
        </div>
    );
}

function DeadlineChip({ deadline }: { deadline: IncidentDeadline }) {
    return (
        <span
            className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-semibold ${DEADLINE_STATUS_CHIP[deadline.status] ?? 'bg-surface-raised text-secondary'}`}
            title={`${deadline.framework_id} · ${deadline.rule} · ${deadline.stage} · ${deadline.due_at ? `due ${formatWhen(deadline.due_at)}` : 'no due time yet'}`}
        >
            {deadline.framework_id} {deadline.stage}: {deadlineText(deadline.status, deadline.hours_remaining)}
        </span>
    );
}

function IncidentDetail({
    incidentId,
    systems,
    systemName,
    frameworks,
    initialChange,
    onChanged,
    onClose,
    onOpenRecord,
    setActiveTab,
}: {
    incidentId: string;
    systems: { id: string; name: string }[];
    systemName: (id: string | null | undefined) => string;
    frameworks: CompliancePageProps['frameworks'];
    initialChange: IncidentChangeResponse | null;
    onChanged: () => void;
    onClose: () => void;
    onOpenRecord: (id: string) => void;
    setActiveTab?: (tab: string) => void;
}) {
    const { gatewayClient } = useGateway();
    const [mode, setMode] = useState<'view' | 'edit' | 'report' | 'close'>('view');
    const [busy, setBusy] = useState(false);
    const [actionError, setActionError] = useState('');
    const [seal, setSeal] = useState<{ what: string; seal: SealStatus } | null>(
        initialChange ? { what: 'incident', seal: initialChange.seal } : null,
    );
    const [authority, setAuthority] = useState('');
    const [reportedAt, setReportedAt] = useState('');
    const [reference, setReference] = useState('');
    const [closeNote, setCloseNote] = useState('');
    const [note, setNote] = useState('');
    /** The staged deadline row whose submission is being recorded (`framework:stage`). */
    const [submitting, setSubmitting] = useState<string | null>(null);
    const [submittedAt, setSubmittedAt] = useState('');

    const loader = useCallback((): Promise<IncidentDetailShape> => gatewayClient.getComplianceIncident(incidentId), [gatewayClient, incidentId]);
    const { data: incident, error, loading, reload } = useKeyedLoad(`incident:${incidentId}`, loader, 'Failed to load the incident');

    const act = async (what: string, fn: () => Promise<IncidentChangeResponse>) => {
        setBusy(true);
        setActionError('');
        try {
            const res = await fn();
            setSeal({ what, seal: res.seal });
            setMode('view');
            reload();
            onChanged();
            return true;
        } catch (e: unknown) {
            setActionError(errorMessage(e, `The ${what} failed`));
            return false;
        } finally {
            setBusy(false);
        }
    };

    if (loading && !incident) return <Loading label="Loading the incident…" testId="incident-loading" />;
    if (error) return <ErrorNote message={error} testId="incident-error" />;
    if (!incident) return null;

    const asIncident: ComplianceIncident = incident;
    const inScope = incident.frameworks ?? [];
    const runIds = incident.linked_run_ids ?? [];
    const inboxIds = incident.linked_inbox_item_ids ?? [];
    const deadlines = incident.deadline_status ?? [];
    const events = incident.events ?? [];

    return (
        <Section
            title={
                <span className="inline-flex items-center gap-2">
                    <Siren className="w-4 h-4 text-primary-600" /> {incident.title}
                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${INCIDENT_STATUS_CHIP[incident.status] ?? ''}`}>{incident.status}</span>
                </span>
            }
            subtitle={`${incident.classification} · ${incident.severity} · ${systemName(incident.ai_system_id)} · ${inScope.join(', ') || 'no framework'} · ${DEADLINES_NOT_ENFORCED}`}
            actions={
                <>
                    {incident.status !== 'closed' && (
                        <>
                            <button type="button" className={BUTTON_SECONDARY} onClick={() => setMode(mode === 'edit' ? 'view' : 'edit')} disabled={busy} data-testid="incident-edit">
                                Edit
                            </button>
                            {incident.status === 'open' && (
                                <button type="button" className={BUTTON_SECONDARY} onClick={() => setMode(mode === 'report' ? 'view' : 'report')} disabled={busy} data-testid="incident-report">
                                    Record report
                                </button>
                            )}
                            <button type="button" className={BUTTON_SECONDARY} onClick={() => setMode(mode === 'close' ? 'view' : 'close')} disabled={busy} data-testid="incident-close">
                                Close incident
                            </button>
                        </>
                    )}
                    <button type="button" className={LINK_BUTTON} onClick={onClose} data-testid="incident-detail-close">
                        Close panel
                    </button>
                </>
            }
            testId="incident-detail"
        >
            <div className="space-y-4 text-xs">
                {seal && <SealNote seal={seal.seal} what={seal.what} onOpenRecord={onOpenRecord} testId="incident-seal" />}
                {actionError && <ErrorNote message={actionError} testId="incident-action-error" />}

                {mode === 'edit' && (
                    <IncidentForm
                        initial={asIncident}
                        frameworks={frameworks}
                        systems={systems}
                        saving={busy}
                        error=""
                        onSubmit={(v) => void act('change', () => gatewayClient.updateComplianceIncident(incidentId, toIncidentUpdateBody(v)))}
                        onCancel={() => setMode('view')}
                    />
                )}
                {mode === 'report' && (
                    <form
                        className="rounded-lg border border-default p-3 space-y-2"
                        onSubmit={(e) => {
                            e.preventDefault();
                            if (!authority.trim()) {
                                setActionError('Name the authority the incident was reported to.');
                                return;
                            }
                            void act('report', () =>
                                gatewayClient.reportComplianceIncident(incidentId, {
                                    authority: authority.trim(),
                                    reported_at: reportedAt ? new Date(reportedAt).toISOString() : null,
                                    external_reference: reference.trim() || null,
                                }),
                            );
                        }}
                        data-testid="incident-report-form"
                    >
                        <p className="text-secondary">Record that the incident was reported to an authority. The report time decides whether each deadline reads on time or late.</p>
                        <div className="flex flex-wrap items-end gap-2">
                            <label className="block">
                                <span className="text-muted uppercase tracking-wide">Authority</span>
                                <input type="text" value={authority} onChange={(e) => setAuthority(e.target.value)} className={`${INPUT_CLASS} block w-56 mt-0.5`} data-testid="incident-authority" />
                            </label>
                            <label className="block">
                                <span className="text-muted uppercase tracking-wide">Reported at (default: now)</span>
                                <input type="datetime-local" value={reportedAt} onChange={(e) => setReportedAt(e.target.value)} className={`${INPUT_CLASS} block mt-0.5`} data-testid="incident-reported-at" />
                            </label>
                            <label className="block">
                                <span className="text-muted uppercase tracking-wide">Reference</span>
                                <input type="text" value={reference} onChange={(e) => setReference(e.target.value)} className={`${INPUT_CLASS} block w-40 mt-0.5`} data-testid="incident-reference" />
                            </label>
                            <button type="submit" className={BUTTON_PRIMARY} disabled={busy} data-testid="incident-report-submit">
                                Record report
                            </button>
                        </div>
                    </form>
                )}
                {mode === 'close' && (
                    <form
                        className="rounded-lg border border-default p-3 space-y-2"
                        onSubmit={(e) => {
                            e.preventDefault();
                            void act('closure', () => gatewayClient.closeComplianceIncident(incidentId, closeNote.trim() || undefined));
                        }}
                        data-testid="incident-close-form"
                    >
                        <label className="block">
                            <span className="text-muted uppercase tracking-wide">Closing note (optional)</span>
                            <textarea value={closeNote} onChange={(e) => setCloseNote(e.target.value)} className={`${INPUT_CLASS} block w-full mt-0.5 h-16`} data-testid="incident-close-note" />
                        </label>
                        <button type="submit" className={BUTTON_PRIMARY} disabled={busy} data-testid="incident-close-submit">
                            Close incident
                        </button>
                    </form>
                )}

                <dl className="grid grid-cols-2 md:grid-cols-4 gap-x-6 gap-y-2" data-testid="incident-facts">
                    <div>
                        <dt className="text-muted uppercase tracking-wide">Became aware</dt>
                        <dd className="text-default">{formatWhen(incident.became_aware_at)}</dd>
                    </div>
                    <div>
                        <dt className="text-muted uppercase tracking-wide">Classified major/significant</dt>
                        <dd className="text-default" data-testid="incident-classified-at-value">
                            {incident.classified_at ? formatWhen(incident.classified_at) : 'not recorded'}
                        </dd>
                    </div>
                    <div>
                        <dt className="text-muted uppercase tracking-wide">Reported</dt>
                        <dd className="text-default">
                            {incident.reported_at ? `${formatWhen(incident.reported_at)} to ${incident.authority ?? '—'}${incident.external_reference ? ` (${incident.external_reference})` : ''}` : 'not yet'}
                        </dd>
                    </div>
                    <div>
                        <dt className="text-muted uppercase tracking-wide">Linked runs</dt>
                        <dd className="text-default">
                            {runIds.length === 0
                                ? '—'
                                : runIds.map((r) => (
                                      <span key={r} className="mr-1.5 font-mono">
                                          {setActiveTab ? (
                                              <button type="button" className="text-primary-600 hover:underline" onClick={() => setActiveTab(`runs:${r}`)}>
                                                  {r.slice(0, 12)}…
                                              </button>
                                          ) : (
                                              `${r.slice(0, 12)}…`
                                          )}
                                      </span>
                                  ))}
                        </dd>
                    </div>
                    <div>
                        <dt className="text-muted uppercase tracking-wide">Linked inbox items</dt>
                        <dd className="text-default">
                            {inboxIds.length === 0 ? (
                                '—'
                            ) : setActiveTab ? (
                                <button type="button" className="text-primary-600 hover:underline" onClick={() => setActiveTab('inbox')}>
                                    {inboxIds.length} item{inboxIds.length === 1 ? '' : 's'} →
                                </button>
                            ) : (
                                `${inboxIds.length} item(s)`
                            )}
                        </dd>
                    </div>
                </dl>
                {incident.description && <p className="text-secondary whitespace-pre-wrap">{incident.description}</p>}

                <div data-testid="incident-deadlines">
                    <h4 className="text-[10px] font-semibold text-muted uppercase tracking-wide mb-1">Deadlines — shown, not enforced</h4>
                    {deadlines.length === 0 ? (
                        <p className="text-muted">No deadline rule of a framework in scope names this classification.</p>
                    ) : (
                        <ul className="space-y-1">
                            {deadlines.map((d) => {
                                const key = `${d.framework_id}:${d.rule}`;
                                const canSubmit = d.staged && !d.submitted_at && d.status !== 'waiting' && incident.status !== 'closed';
                                return (
                                    <li key={key} className="space-y-1" data-testid={`incident-deadline-${d.framework_id}-${d.rule}`}>
                                        <div className="flex items-center gap-2 flex-wrap">
                                            <DeadlineChip deadline={d} />
                                            <span className="text-secondary">
                                                {d.rule.replace(/_/g, ' ')} · {d.due_at ? `due ${formatWhen(d.due_at)}` : 'no due time yet'}
                                                {d.submitted_at ? ` · submitted ${formatWhen(d.submitted_at)}` : ''}
                                            </span>
                                            <span className="text-muted" data-testid={`incident-deadline-anchor-${d.framework_id}-${d.rule}`}>
                                                {deadlineAnchorText(d)}
                                            </span>
                                            {canSubmit && submitting !== key && (
                                                <button
                                                    type="button"
                                                    className={LINK_BUTTON}
                                                    disabled={busy}
                                                    onClick={() => {
                                                        setSubmitting(key);
                                                        setSubmittedAt(toLocalInput(new Date().toISOString()));
                                                    }}
                                                    data-testid={`incident-submit-${d.framework_id}-${d.rule}`}
                                                >
                                                    Record submission
                                                </button>
                                            )}
                                        </div>
                                        {canSubmit && submitting === key && (
                                            <form
                                                className="flex flex-wrap items-end gap-2 pl-2"
                                                onSubmit={(e) => {
                                                    e.preventDefault();
                                                    void act('submission', () =>
                                                        gatewayClient.recordComplianceIncidentSubmission(incidentId, {
                                                            framework_id: d.framework_id,
                                                            stage: d.rule,
                                                            submitted_at: submittedAt ? new Date(submittedAt).toISOString() : null,
                                                        }),
                                                    ).then((ok) => {
                                                        if (ok) setSubmitting(null);
                                                    });
                                                }}
                                                data-testid={`incident-submit-form-${d.framework_id}-${d.rule}`}
                                            >
                                                <label className="block">
                                                    <span className="text-muted uppercase tracking-wide">
                                                        {d.framework_id} {d.rule.replace(/_/g, ' ')} submitted at
                                                    </span>
                                                    <input
                                                        type="datetime-local"
                                                        value={submittedAt}
                                                        onChange={(e) => setSubmittedAt(e.target.value)}
                                                        className={`${INPUT_CLASS} block mt-0.5`}
                                                        data-testid={`incident-submitted-at-${d.framework_id}-${d.rule}`}
                                                    />
                                                </label>
                                                <button type="submit" className={BUTTON_PRIMARY} disabled={busy} data-testid={`incident-submit-save-${d.framework_id}-${d.rule}`}>
                                                    Record
                                                </button>
                                                <button type="button" className={LINK_BUTTON} onClick={() => setSubmitting(null)}>
                                                    Cancel
                                                </button>
                                            </form>
                                        )}
                                    </li>
                                );
                            })}
                        </ul>
                    )}
                </div>

                <div data-testid="incident-events">
                    <h4 className="text-[10px] font-semibold text-muted uppercase tracking-wide mb-1">Sealed history</h4>
                    <ol className="divide-y divide-default">
                        {events.map((ev) => (
                            <li key={ev.id} className="py-1.5 flex items-center gap-3 flex-wrap" data-testid={`incident-event-${ev.id}`}>
                                <span className="text-secondary whitespace-nowrap">{formatWhen(ev.at)}</span>
                                <span className="text-default">{incidentEventText(ev.event, ev.detail)}</span>
                                {ev.actor_id && <span className="text-muted">{ev.actor_id}</span>}
                                {ev.record_id ? (
                                    <RecordLink recordId={ev.record_id} onOpen={onOpenRecord} />
                                ) : (
                                    <span className="text-amber-800">not sealed</span>
                                )}
                            </li>
                        ))}
                    </ol>
                    {incident.status !== 'closed' && (
                        <form
                            className="mt-2 flex items-end gap-2"
                            onSubmit={(e) => {
                                e.preventDefault();
                                if (!note.trim()) return;
                                void act('note', () => gatewayClient.addComplianceIncidentNote(incidentId, note.trim())).then((ok) => {
                                    if (ok) setNote('');
                                });
                            }}
                            data-testid="incident-note-form"
                        >
                            <label className="block flex-1">
                                <span className="text-muted uppercase tracking-wide">Add a note</span>
                                <input type="text" value={note} onChange={(e) => setNote(e.target.value)} className={`${INPUT_CLASS} block w-full mt-0.5`} data-testid="incident-note" />
                            </label>
                            <button type="submit" className={BUTTON_SECONDARY} disabled={busy || !note.trim()} data-testid="incident-note-submit">
                                Add note
                            </button>
                        </form>
                    )}
                </div>
            </div>
        </Section>
    );
}

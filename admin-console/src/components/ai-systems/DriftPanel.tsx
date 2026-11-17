/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Drift findings for one AI System.
 *
 * **The cause comes first.** Every row leads with the plain-language
 * explanation and puts the statistic underneath, because an alert that
 * opens with "z = 8.4, p < 0.001" hands the investigation back to the
 * person reading it. That ordering is the entire point of the phase, and
 * it is a UI decision as much as a backend one.
 *
 * Three other things this panel is careful about:
 *
 *   - **Learning is not healthy.** A system still gathering runs shows
 *     "ready in ~N runs", never an empty list that reads as "no problems".
 *   - **Unexplained says so.** No invented cause; the correlated metrics
 *     are offered as a starting point instead.
 *   - **Closing says what kind of fact it is**. One "Close…"
 *     action opens a dialog with the three kinds — accepted change, cause
 *     fixed, false positive — each stating its effect on detection, because
 *     the detector honours it: a closed finding cannot re-fire from the same
 *     samples. "Investigating" (acknowledge) stays a separate action that
 *     never closes. A closed card says which kind it was and what that did
 *     ("baseline re-learning since …", "incident window … → …").
 */

import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, CheckCircle2, RefreshCw } from 'lucide-react';

import { HelpTip } from '../ui/HelpTip';

import { useGateway } from '../../hooks/useGateway';
import { useAISystemNames } from '../../hooks/useAISystemNames';
import AISystemNamesNotice from './AISystemNamesNotice';
import {
    ALL_CALLERS,
    Baseline,
    CAUSE_LABELS,
    DRIFT_CLASS_LABELS,
    DRIFT_METRIC_EXPLAINERS,
    DRIFT_SEVERITY_CLASSES,
    DriftEvent,
    DriftListResponse,
    SIGNAL_BASIS_LABELS,
    driftMetricLabel,
} from '../../lib/aiSystemTypes';
import { aiSystemLink } from '../../lib/composition';
import { driftDependency } from '../../lib/dependencyHealth';
import type { ReleaseChange } from '../../lib/agentRelease';
import { agentImplementationLink, releaseChangeOf, releaseChangeText } from '../../lib/agentRelease';
import { TrustBadge } from '../agent/AgentReleaseBadges';
import type { BaselineEpoch, CloseDriftRequest, IncidentWindow } from '../../lib/driftResolutionTypes';
import {
    closedEffect,
    epochText,
    incidentWindowCause,
    incidentWindowCauseText,
} from '../../lib/driftResolution';
import { errorKind, errorMessage } from '../../lib/narrow';
import ResolveDriftDialog from './ResolveDriftDialog';
import { driftOutcomeBucket } from '../../lib/runOutcomes';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

interface DriftPanelProps {
    aiSystemId: string;
    /** Fired after a triage lands, so the pinned status strip's open-count
     *  can follow the operator's verdict instead of going stale. */
    onChanged?: () => void;
    /** Open another page (App tab id) — a dependency finding's dependency. */
    setActiveTab?: (tab: string) => void;
}

export function DriftPanel({ aiSystemId, onChanged, setActiveTab }: DriftPanelProps) {
    const [readFailure, reads] = useReadFailures<'epochs' | 'windows'>();
    const {
        fetchAISystemDrift,
        fetchAISystemBaselines,
        fetchAISystemBaselineEpochs,
        fetchAISystemIncidentWindows,
        acknowledgeDriftEvent,
        closeDriftEvent,
    } = useGateway();

    const [drift, setDrift] = useState<DriftListResponse | null>(null);
    const [baselines, setBaselines] = useState<Baseline[]>([]);
    const [learning, setLearning] = useState(0);
    // The effects closed findings had: epochs and windows carry
    // the finding's id, which is how a closed card names its effect.
    const [epochs, setEpochs] = useState<BaselineEpoch[]>([]);
    const [windows, setWindows] = useState<IncidentWindow[]>([]);
    const [loading, setLoading] = useState(true);
    const [busy, setBusy] = useState('');
    const [error, setError] = useState('');
    const [closing, setClosing] = useState<{
        events: DriftEvent[];
        title: string;
        /** The window an earlier (partial) cause_fixed close of this card
         *  declared — a retry attaches to it rather than declaring another. */
        windowId?: string | null;
    } | null>(null);

    const load = useCallback(async () => {
        try {
            setLoading(true);
            setError('');
            const [d, b, ep, iw] = await Promise.all([
                fetchAISystemDrift(aiSystemId, 720),
                fetchAISystemBaselines(aiSystemId),
                // Best-effort: a closed card without its effect still says
                // how it was closed; the findings must not wait on these.
                fetchAISystemBaselineEpochs(aiSystemId).then(
                    (answer) => {
                        reads.loaded('epochs');
                        return answer;
                    },
                    (err: unknown) => {
                        reads.lost('epochs', 'Failed to load the baseline epochs; closed findings do not show their effect', err);
                        return null;
                    },
                ),
                fetchAISystemIncidentWindows(aiSystemId).then(
                    (answer) => {
                        reads.loaded('windows');
                        return answer;
                    },
                    (err: unknown) => {
                        reads.lost('windows', 'Failed to load the incident windows; closed findings do not show their effect', err);
                        return null;
                    },
                ),
            ]);
            setDrift(d);
            setBaselines(b.baselines);
            setLearning(b.learning);
            setEpochs(ep?.epochs ?? []);
            setWindows(iw?.windows ?? []);
        } catch (err: unknown) {
            console.error('Failed to load drift:', errorKind(err));
            setError(errorMessage(err, 'Failed to load drift'));
            setDrift(null);
        } finally {
            setLoading(false);
        }
    }, [aiSystemId, fetchAISystemDrift, fetchAISystemBaselines, fetchAISystemBaselineEpochs, fetchAISystemIncidentWindows, reads]);

    useEffect(() => {
        void load();
    }, [load]);

    const acknowledge = async (ids: string[]) => {
        try {
            setBusy(ids[0]);
            // A grouped card triages every member: the operator is judging
            // the finding, not one of its five identical instances.
            for (const id of ids) {
                await acknowledgeDriftEvent(id);
            }
            await load();
            onChanged?.();
        } catch (err: unknown) {
            setError(errorMessage(err, 'Failed to update finding'));
        } finally {
            setBusy('');
        }
    };

    /** Close every open member of the card with the same kind and note.
     *
     *  cause_fixed on a grouped card is ONE incident: the first member's
     *  close declares the window, every other member attaches to it by
     *  `incident_window_id` — never N identical windows.
     *
     *  Every member is attempted; a refusal is recorded per member and the
     *  loop goes on. If any member failed, the dialog stays open on the
     *  members still open, names each failure, and a retry attaches to the
     *  window already declared. */
    const close = async (
        events: DriftEvent[],
        request: CloseDriftRequest,
        existingWindowId: string | null = null,
    ) => {
        const open = events.filter((e) => e.is_open);
        if (open.length === 0) return;
        setBusy(open[0].id);
        let windowId = existingWindowId;
        const closedIds = new Set<string>();
        const failures: { id: string; reason: string }[] = [];
        try {
            for (const e of open) {
                const req: CloseDriftRequest =
                    request.kind === 'cause_fixed' && windowId
                        ? { kind: 'cause_fixed', note: request.note, incident_window_id: windowId }
                        : request;
                try {
                    const res = await closeDriftEvent(e.id, req);
                    closedIds.add(e.id);
                    if (request.kind === 'cause_fixed' && !windowId) {
                        windowId = res.incident_window?.id ?? null;
                    }
                } catch (err: unknown) {
                    failures.push({ id: e.id, reason: errorMessage(err, 'refused') });
                }
            }
        } finally {
            setBusy('');
        }
        if (closedIds.size > 0) {
            await load();
            onChanged?.();
        }
        if (failures.length === 0) {
            setClosing(null);
            return;
        }
        setClosing((c) =>
            c ? { ...c, events: c.events.filter((e) => !closedIds.has(e.id)), windowId } : c,
        );
        throw new Error(
            `Closed ${closedIds.size} of ${open.length}. Not closed: ` +
                failures.map((f) => `${f.id} (${f.reason})`).join('; '),
        );
    };

    // Baselines re-learning after a human resolution — the aggregate rows
    // only; a per-caller sub-baseline inherits the aggregate's epoch.
    const relearning = baselines.filter(
        (b) =>
            b.epoch &&
            b.bucket === 'all' &&
            (b.actor_id ?? ALL_CALLERS) === ALL_CALLERS &&
            (b.caller_system_id ?? ALL_CALLERS) === ALL_CALLERS,
    );

    const stillLearning = baselines.length > 0 && learning === baselines.length;
    // Per-caller findings on a shared service name the caller.
    const systemNames = useAISystemNames(
        (drift?.events ?? []).some((e) => Boolean(e.caller_system_id)),
    );
    const { names } = systemNames;

    return (
        <div className="space-y-4" data-testid="drift-panel">
            <div className="flex items-start justify-between gap-4">
                <div className="flex items-start gap-2">
                    <h3 className="text-sm font-semibold text-default">Behavioural drift</h3>
                    <HelpTip text="Whether this system behaves differently from how it used to — and why. Compared against its own history, not against other systems." />
                </div>
                <button
                    type="button"
                    onClick={() => void load()}
                    className="flex items-center gap-2 px-3 py-1.5 text-sm border border-strong rounded-lg hover:bg-surface-hover"
                >
                    <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
                    Refresh
                </button>
            </div>

            {error && (
                <div className="flex items-start gap-2 p-3 border border-red-200 bg-red-50 rounded">
                    <AlertTriangle className="w-4 h-4 text-red-600 shrink-0 mt-0.5" />
                    <span className="text-sm text-red-800">{error}</span>
                </div>
            )}
            <ReadFailureNotice failure={readFailure('epochs')} onRetry={() => void load()} />
            <ReadFailureNotice failure={readFailure('windows')} onRetry={() => void load()} />

            {/* Learning is not the same as healthy, and must not look like it. */}
            {stillLearning && !error && (
                <div
                    className="p-3 border border-default rounded-lg bg-surface-subtle"
                    data-testid="drift-learning"
                >
                    <p className="text-sm text-default font-medium">
                        Still learning this system&apos;s normal
                    </p>
                    <p className="text-sm text-muted mt-1">
                        Drift detection compares a system against its own history, so it
                        needs enough runs before it can say anything useful. Nothing is
                        being monitored for drift yet — this is not a clean bill of health.
                    </p>
                </div>
            )}

            {drift && !error && drift.events.length === 0 && !stillLearning && (
                <div className="p-6 text-center border border-dashed border-strong rounded-lg">
                    <p className="text-sm text-default font-medium">
                        No drift detected
                    </p>
                    <p className="text-sm text-muted mt-1">
                        This system is behaving consistently with its own baseline over the
                        last 30 days.
                    </p>
                </div>
            )}

            {drift && drift.unexplained_count > 0 && (
                <p className="text-xs text-muted" data-testid="unexplained-count">
                    {drift.unexplained_count} of {drift.open_count} open finding(s) have no
                    identified cause.
                </p>
            )}

            {relearning.length > 0 && !error && (
                <div
                    className="p-3 border border-default rounded-lg bg-surface-subtle space-y-1"
                    data-testid="drift-epochs"
                >
                    <p className="text-sm text-default font-medium">Baselines reset by a resolution</p>
                    <ul className="space-y-0.5">
                        {relearning.map((b) => (
                            <li
                                key={b.id}
                                className="text-xs text-secondary"
                                data-testid={`baseline-epoch-${b.metric_key}`}
                            >
                                <span className="text-default">{driftMetricLabel(b.metric_key, b.basis)}</span>
                                {' — '}
                                {b.epoch ? epochText(b.epoch) : ''}
                                {/* Learning is not healthy: the state stays the
                                    baseline's own, never rounded up to ready. */}
                                <span
                                    className={`ml-2 inline-flex px-1.5 py-0.5 rounded font-medium ${
                                        b.is_ready ? 'bg-green-100 text-green-800' : 'bg-blue-100 text-blue-800'
                                    }`}
                                    data-testid="baseline-epoch-state"
                                >
                                    {b.is_ready
                                        ? `ready · ${b.sample_count} samples since`
                                        : `learning · ${b.sample_count} samples so far`}
                                </span>
                            </li>
                        ))}
                    </ul>
                </div>
            )}

            <AISystemNamesNotice names={systemNames} className="block" />
            {groupEvents(drift?.events ?? []).map((g) => (
                <DriftRow
                    key={g.representative.id}
                    event={g.representative}
                    count={g.ids.length}
                    busy={busy === g.ids[0]}
                    onAcknowledge={() => void acknowledge(g.ids)}
                    onClose={(title) =>
                        setClosing({
                            events: (drift?.events ?? []).filter((e) => g.ids.includes(e.id)),
                            title,
                        })
                    }
                    epochs={epochs}
                    windows={windows}
                    names={names}
                    open={setActiveTab}
                />
            ))}

            {closing && (
                <ResolveDriftDialog
                    events={closing.events}
                    title={closing.title}
                    onConfirm={(request) => close(closing.events, request, closing.windowId ?? null)}
                    onCancel={() => setClosing(null)}
                />
            )}
        </div>
    );
}

/** Fold identical findings into one card with a multiplier.

 *  The detector emits one event per sweep; a standing condition therefore
 *  stacks near-identical cards until the panel is nothing but repetition —
 *  the UI edition of alert fatigue. Grouping
 *  is presentational only: every underlying event is kept, and a triage on
 *  the card applies to all of them. */
function groupEvents(events: DriftEvent[]): {
    representative: DriftEvent;
    ids: string[];
}[] {
    const groups = new Map<string, { representative: DriftEvent; ids: string[] }>();
    for (const e of events) {
        const key = [
            e.attributed_cause,
            e.drift_class,
            e.metric_key,
            // A per-caller finding and the aggregate one are different
            // findings, as are the same metric on runs vs served calls.
            e.basis ?? 'runs',
            e.caller_system_id ?? '',
            e.direction,
            e.severity,
            e.status,
        ].join('|');
        const g = groups.get(key);
        if (g) {
            g.ids.push(e.id);
            // Keep the newest as the face of the group.
            if ((e.detected_at ?? '') > (g.representative.detected_at ?? '')) {
                g.representative = e;
            }
        } else {
            groups.set(key, { representative: e, ids: [e.id] });
        }
    }
    return [...groups.values()];
}

function DriftRow({
    event,
    count = 1,
    busy,
    onAcknowledge,
    onClose,
    epochs = [],
    windows = [],
    names = {},
    open,
}: {
    event: DriftEvent;
    count?: number;
    busy: boolean;
    onAcknowledge: () => void;
    /** Open the close dialog; receives the card's title. */
    onClose: (title: string) => void;
    epochs?: BaselineEpoch[];
    windows?: IncidentWindow[];
    names?: Record<string, string>;
    open?: (tab: string) => void;
}) {
    const causeLabel = CAUSE_LABELS[event.attributed_cause] ?? event.attributed_cause;
    // The correlator's summary, when it has one that adds information —
    // repeating the cause label as its own subtitle told the operator
    // nothing twice.
    const causeSummary = (event.cause_evidence?.summary as string | undefined) ?? '';
    // Rung 0: the change lies at the edge of a declared window.
    const inWindow = event.attributed_cause === 'incident_window' ? incidentWindowCause(event.cause_evidence) : null;
    const dependency = driftDependency(event);
    const metricTitle = driftMetricLabel(event.metric_key, event.basis, dependency?.name);
    const onSegments = event.basis === 'segments';
    const caller = event.caller_system_id
        ? names[event.caller_system_id] ?? event.caller_system_id
        : null;
    const directionWord =
        event.direction === 'up' ? ' up' : event.direction === 'down' ? ' down' : '';
    // The agent_release cause names both releases; an agent
    // finding (drift_class `agent`) is a governance finding, not a shift.
    const release = event.attributed_cause === 'agent_release' ? releaseChangeOf(event.cause_evidence) : null;
    const agentFinding = event.drift_class === 'agent';
    // A finding learned on one outcome's runs names that outcome:
    // the work of one kind moved, not the mix.
    const bucket = driftOutcomeBucket(event.cause_evidence);
    const title = dependency
        ? `${metricTitle} is not healthy`
        : agentFinding
          ? `${metricTitle}${release?.agentName ? ` — ${release.agentName}` : ''}`
          : `${metricTitle} shifted${directionWord}${bucket ? ` in ${bucket} runs` : ''}`;

    return (
        <div
            className="bg-surface border border-default rounded-lg p-4 space-y-3"
            data-testid={`drift-event-${event.id}`}
        >
            <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                    {/* WHAT shifted leads; the cause is a labeled line below.
                        Leading with "No cause identified" made the absence of
                        an explanation look like the finding itself. */}
                    <p className="text-sm font-medium text-default" data-testid="drift-title">
                        {/* A dependency finding is a state of the dependency,
                            not a shift in a metric of this system. */}
                        {title}
                        {count > 1 && (
                            <span
                                className="ml-2 inline-flex px-1.5 py-0.5 rounded bg-surface-subtle text-xs text-secondary align-middle"
                                data-testid="drift-group-count"
                                title={`${count} findings with the same cause, metric and severity — triage applies to all of them`}
                            >
                                ×{count}
                            </span>
                        )}
                        {onSegments && (
                            <span
                                className="ml-2 inline-flex px-1.5 py-0.5 rounded bg-surface-subtle text-xs text-secondary align-middle"
                                data-testid="drift-basis"
                                title="Measured against this system's served calls as a shared service, not its own runs"
                            >
                                served calls{caller ? ` from ${caller}` : ''}
                            </span>
                        )}
                    </p>
                    {dependency && (
                        <p className="text-sm text-muted mt-1" data-testid="drift-dependency">
                            A dependency this system relies on is failing — not this
                            system&apos;s own behaviour.{' '}
                            {open ? (
                                <button
                                    type="button"
                                    className="text-primary-700 hover:underline"
                                    onClick={() => open(aiSystemLink(dependency.id, 'signals'))}
                                    data-link={aiSystemLink(dependency.id, 'signals')}
                                    data-testid="drift-dependency-link"
                                >
                                    Open {dependency.name ?? dependency.id}
                                </button>
                            ) : (
                                <span className="text-secondary">{dependency.name ?? dependency.id}</span>
                            )}
                        </p>
                    )}
                    {DRIFT_METRIC_EXPLAINERS[event.metric_key] && (
                        <p className="text-sm text-muted mt-1">
                            {DRIFT_METRIC_EXPLAINERS[event.metric_key]}
                        </p>
                    )}
                    <p className="text-sm text-secondary mt-1" data-testid="drift-cause">
                        <span className="text-muted">Cause:</span> {causeLabel}
                        {causeSummary && causeSummary !== causeLabel && ` — ${causeSummary}`}
                    </p>
                    {inWindow && (
                        <p className="text-xs text-secondary mt-0.5" data-testid="drift-cause-window">
                            {incidentWindowCauseText(inWindow)} — runs inside the window are not judged.
                        </p>
                    )}
                    {release && <ReleaseCause change={release} open={open} />}
                </div>
                <span
                    className={`shrink-0 inline-flex px-2 py-0.5 rounded text-xs font-medium ${
                        DRIFT_SEVERITY_CLASSES[event.severity] ?? 'bg-slate-100 text-slate-700'
                    }`}
                >
                    {event.severity}
                </span>
            </div>

            {/* The wire slug stays visible — it is what an operator greps
                logs and baselines for. */}
            <div className="text-xs text-muted" data-testid="drift-detail">
                {DRIFT_CLASS_LABELS[event.drift_class] ?? event.drift_class} ·{' '}
                {event.metric_key} {event.direction ?? ''}
                {event.basis && (
                    <span data-testid="drift-detail-basis">
                        {' · on '}
                        {SIGNAL_BASIS_LABELS[event.basis]}
                    </span>
                )}
                {caller && (
                    <span data-testid="drift-caller">
                        {' · caller '}
                        {caller}
                    </span>
                )}
                {event.baseline_value !== null && event.baseline_value !== undefined && (
                    <>
                        {' · '}
                        {event.baseline_value.toFixed(4)} →{' '}
                        {(event.observed_value ?? 0).toFixed(4)}
                    </>
                )}
                {event.change_point_at && (
                    <>
                        {' · changed '}
                        {new Date(event.change_point_at).toLocaleString()}
                    </>
                )}
            </div>

            {!event.is_explained && (
                <p className="text-xs text-amber-700" data-testid="drift-unexplained">
                    No change to the model, tools, composition or inputs lines up with
                    this. Worth looking at manually.
                </p>
            )}

            {event.is_open && (
                <div className="flex flex-wrap gap-2 pt-1">
                    {/* Acknowledge never closes — it says someone is on it. */}
                    {event.status === 'active' && (
                        <button
                            type="button"
                            disabled={busy}
                            onClick={onAcknowledge}
                            className="px-3 py-1.5 text-xs border border-strong rounded-lg hover:bg-surface-hover disabled:opacity-50"
                            data-testid="ack-button"
                        >
                            Investigating
                        </button>
                    )}
                    <button
                        type="button"
                        disabled={busy}
                        onClick={() => onClose(title)}
                        title="Say what kind of fact this is — accepted change, cause fixed or false positive. Detection honours it."
                        className="flex items-center gap-1.5 px-3 py-1.5 text-xs border border-strong rounded-lg hover:bg-surface-hover disabled:opacity-50"
                        data-testid="close-finding-button"
                    >
                        <CheckCircle2 className="w-3.5 h-3.5" />
                        Close…
                    </button>
                </div>
            )}

            {!event.is_open && <ClosedLine event={event} epochs={epochs} windows={windows} />}
        </div>
    );
}

/** How a closed finding was closed and what that did to
 *  detection, with the operator's note. */
function ClosedLine({
    event,
    epochs,
    windows,
}: {
    event: DriftEvent;
    epochs: BaselineEpoch[];
    windows: IncidentWindow[];
}) {
    const { label, effect } = closedEffect(event, epochs, windows);
    return (
        <div className="text-xs text-muted space-y-0.5" data-testid="drift-closed">
            <p>
                <span className="font-medium text-secondary" data-testid="drift-closed-kind">
                    {label}
                </span>
                {event.resolved_by ? ` by ${event.resolved_by}` : ''}
                {event.resolved_at ? ` · ${new Date(event.resolved_at).toLocaleString()}` : ''}
                {effect && <span data-testid="drift-closed-effect">{` — ${effect}`}</span>}
            </p>
            {event.resolution_note && <p className="text-secondary">“{event.resolution_note}”</p>}
        </div>
    );
}

/** The releases behind an `agent_release` cause: from → to, or
 *  "no version change, new fingerprint" (the undeclared change). */
function ReleaseCause({ change, open }: { change: ReleaseChange; open?: (tab: string) => void }) {
    const text = releaseChangeText(change);
    const agentId = change.agentId;
    return (
        <div className="mt-1 space-y-0.5 text-sm" data-testid="drift-release">
            {change.code && change.message && (
                <p className="text-secondary" data-testid="drift-release-message">
                    {change.message}
                </p>
            )}
            {text && (
                <p
                    className={change.versionChanged ? 'text-secondary' : 'font-medium text-red-700'}
                    data-testid={change.versionChanged ? 'drift-release-transition' : 'drift-release-no-version-change'}
                >
                    <span className="text-muted font-normal">Release:</span> {text}
                </p>
            )}
            <p className="flex flex-wrap items-center gap-2 text-xs">
                {change.trust && <TrustBadge trust={change.trust} testId="drift-release-trust" />}
                {change.changedAt && (
                    <span className="text-muted">changed {new Date(change.changedAt).toLocaleString()}</span>
                )}
                {agentId && open && (
                    <button
                        type="button"
                        className="text-primary-700 hover:underline"
                        onClick={() => open(agentImplementationLink(agentId))}
                        data-link={agentImplementationLink(agentId)}
                        data-testid="drift-release-link"
                    >
                        Open the agent&apos;s releases
                    </button>
                )}
            </p>
        </div>
    );
}

export default DriftPanel;

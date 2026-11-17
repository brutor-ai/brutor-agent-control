/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Liveness for one AI System.
 *
 * The only card in the product that can say something about a system
 * producing *nothing*. Every other panel is a function of traffic that
 * arrived, so a dead system reads as perfectly healthy on all of them.
 *
 * The verdict is the control plane's ONE liveness state:
 * alive · silent · idle · unstarted · not configured, rendered through
 * `lib/livenessState.ts` with the server's reason under it. Only silent is
 * a finding and only alive is green; idle (on demand, nobody called),
 * unstarted (never ran) and not configured are never rounded up.
 *
 * Two timestamps, not one: the last run (any outcome: the agent
 * is alive and checking) and the last PROCESSED run (an outcome that counts
 * toward the baseline: work is getting done). A system that keeps polling
 * while an upstream feed has stopped delivering is alive and idle at once;
 * the optional "no processed run for" bound turns that into its own state,
 * `work quiet` — never silence.
 */

import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, Hourglass, Sparkles } from 'lucide-react';

import { HelpTip } from '../ui/HelpTip';
import { LivenessBadge } from './LivenessBadge';

import { useGateway } from '../../hooks/useGateway';
import {
    CadenceProposal,
    humanizeSeconds,
    LIVENESS_MODE_DESCRIPTIONS,
    LIVENESS_MODE_LABELS,
    LivenessMode,
    LivenessStatus,
} from '../../lib/aiSystemTypes';
import { QUIET_UNITS, parseQuietBound, quietBoundParts, type QuietUnit } from '../../lib/runOutcomes';
import { errorKind, errorMessage } from '../../lib/narrow';

interface LivenessCardProps {
    aiSystemId: string;
}

export function LivenessCard({ aiSystemId }: LivenessCardProps) {
    const {
        fetchAISystemLiveness,
        updateAISystemLiveness,
        learnAISystemLiveness,
        acceptAISystemLiveness,
    } = useGateway();

    const [status, setStatus] = useState<LivenessStatus | null>(null);
    const [proposal, setProposal] = useState<CadenceProposal | null>(null);
    const [loading, setLoading] = useState(true);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');

    const load = useCallback(async () => {
        try {
            setLoading(true);
            setError('');
            setStatus(await fetchAISystemLiveness(aiSystemId));
        } catch (err: unknown) {
            console.error('Failed to load liveness:', errorKind(err));
            setError(err instanceof Error ? err.message : 'Failed to load liveness');
            setStatus(null);
        } finally {
            setLoading(false);
        }
    }, [aiSystemId, fetchAISystemLiveness]);

    useEffect(() => {
        void load();
    }, [load]);

    const onLearn = async () => {
        try {
            setBusy(true);
            setError('');
            setProposal(await learnAISystemLiveness(aiSystemId));
        } catch (err: unknown) {
            setError(err instanceof Error ? err.message : 'Failed to learn cadence');
        } finally {
            setBusy(false);
        }
    };

    const onAccept = async () => {
        try {
            setBusy(true);
            setError('');
            await acceptAISystemLiveness(aiSystemId);
            setProposal(null);
            await load();
        } catch (err: unknown) {
            setError(err instanceof Error ? err.message : 'Failed to accept cadence');
        } finally {
            setBusy(false);
        }
    };

    const onModeChange = async (mode: LivenessMode) => {
        try {
            setBusy(true);
            setError('');
            await updateAISystemLiveness(aiSystemId, { mode });
            await load();
        } catch (err: unknown) {
            setError(err instanceof Error ? err.message : 'Failed to update mode');
        } finally {
            setBusy(false);
        }
    };

    const onBoundChange = async (seconds: number | null) => {
        try {
            setBusy(true);
            setError('');
            await updateAISystemLiveness(aiSystemId, { max_quiet_processed_seconds: seconds });
            await load();
        } catch (err: unknown) {
            setError(errorMessage(err, 'Failed to update the no-processed-run bound'));
        } finally {
            setBusy(false);
        }
    };

    if (loading && !status) {
        return (
            <div className="p-6 border border-default rounded-lg text-sm text-muted">
                Loading liveness…
            </div>
        );
    }

    return (
        <div className="bg-surface border border-default rounded-lg p-5 space-y-4" data-testid="liveness-card">
            <div className="grid grid-cols-[1fr_auto_1fr] items-start gap-4">
                <div className="flex items-start gap-2">
                    <h3 className="text-sm font-semibold text-default">Liveness</h3>
                    {/* Unlike the observed signals below, this card is an
                        EXPECTATION the operator sets — mode, window, grace
                        are all editable, and "Suggest a cadence" fills
                        them from history. The chip pairs with the
                        "observed" chips on the signal sections. */}
                    <span
                        className="px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wider bg-primary-50 text-primary-700 border border-primary-200"
                        data-testid="liveness-editable-chip"
                    >
                        expectation · editable
                    </span>
                    <HelpTip text="The only signal that fires when a system stops calling. Every other metric is a function of traffic that arrived, so a dead system looks healthy on all of them. This card is configurable: the mode, window and grace are the expectation you set, and the platform alerts against it." />
                </div>
                {/* The verdict sits in the middle of the header, between the
                    title and the actions, where the eye lands first. */}
                <div className="justify-self-center" data-testid="liveness-status-slot">
                    {status && <StatusPill status={status} />}
                </div>
                <div />
            </div>

            {error && (
                <div className="flex items-start gap-2 p-3 border border-red-200 bg-red-50 rounded">
                    <AlertTriangle className="w-4 h-4 text-red-600 shrink-0 mt-0.5" />
                    <span className="text-sm text-red-800">{error}</span>
                </div>
            )}

            {status && (
                <>
                    {/* Mode */}
                    <div>
                        <label className="flex items-center gap-1.5 text-xs text-muted uppercase tracking-wider mb-1">
                            Mode
                            {/* The line below the select describes the *selected*
                                mode. This describes the choice itself — which an
                                operator needs before changing it, and which is
                                the difference between a page at 3am and silence
                                nobody notices. */}
                            <HelpTip
                                title="Liveness mode"
                                ariaLabel="What does mode mean?"
                                text={
                                    'How this system is expected to produce traffic — it sets ' +
                                    'what counts as suspicious silence. Continuous: a steady ' +
                                    'stream, so any gap past the window plus grace is a finding. ' +
                                    'Scheduled: at least one run per window, and a window that ' +
                                    'passes with none raises a high-severity missed schedule — ' +
                                    'something was due at a known time and did not happen. ' +
                                    'Sporadic: runs irregularly, so quiet is never a finding and ' +
                                    'only outright failures are. On demand: liveness is not ' +
                                    'monitored at all. Choose by how the system is driven, not ' +
                                    'by how important it is: marking a batch job continuous ' +
                                    'pages you between runs, and marking it sporadic means ' +
                                    'nobody is told when it stops for good.'
                                }
                            />
                        </label>
                        <select
                            aria-label="Liveness mode"
                            value={status.expectation.mode}
                            disabled={busy}
                            onChange={(e) => void onModeChange(e.target.value as LivenessMode)}
                            className="w-full px-3 py-2 border border-strong rounded-lg bg-surface text-sm"
                        >
                            {(Object.keys(LIVENESS_MODE_LABELS) as LivenessMode[]).map((m) => (
                                <option key={m} value={m}>
                                    {LIVENESS_MODE_LABELS[m]}
                                </option>
                            ))}
                        </select>
                        <p className="text-xs text-muted mt-1">
                            {LIVENESS_MODE_DESCRIPTIONS[status.expectation.mode]}
                        </p>
                        {!status.configured && (
                            <p className="text-xs text-muted mt-1 italic" data-testid="liveness-default-note">
                                Showing the default for this kind of system — nothing has been
                                saved yet.
                            </p>
                        )}
                    </div>

                    {/* Window */}
                    {status.expectation.alerts_on_silence && (
                        <div className="grid grid-cols-3 gap-3" data-testid="liveness-window">
                            <Fact label="Window" value={humanizeSeconds(status.expectation.window_seconds)} />
                            <Fact label="Grace" value={humanizeSeconds(status.expectation.grace_seconds)} />
                            <Fact
                                label="Min runs"
                                value={String(status.expectation.expected_min_runs)}
                            />
                        </div>
                    )}

                    {/* Observed — any run, and a run that did the work. */}
                    <div className="grid grid-cols-2 gap-3" data-testid="liveness-observed">
                        <Fact
                            label="Last run"
                            value={
                                status.last_run_at
                                    ? new Date(status.last_run_at).toLocaleString()
                                    : 'never'
                            }
                            help="The latest run of any outcome — the agent is alive and checking, even when it found nothing to do."
                        />
                        <Fact label="Quiet for" value={humanizeSeconds(status.quiet_for_seconds)} />
                        <Fact
                            label="Last processed run"
                            value={
                                status.last_processed_run_at
                                    ? new Date(status.last_processed_run_at).toLocaleString()
                                    : 'never'
                            }
                            help="The latest run that completed in a declared outcome that counts toward the baseline — work getting done, not only the agent checking for it."
                            testId="liveness-last-processed"
                        />
                        <Fact
                            label="No work for"
                            value={humanizeSeconds(status.processed_quiet_for_seconds)}
                            testId="liveness-processed-quiet"
                        />
                    </div>

                    {status.work_quiet && (
                        <div
                            className="flex items-start gap-2 p-3 border border-amber-200 bg-amber-50 rounded"
                            data-testid="liveness-work-quiet"
                        >
                            <Hourglass className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
                            <div className="text-sm text-amber-900">
                                <p className="font-medium">Work quiet</p>
                                <p className="mt-0.5">
                                    {status.work_quiet_reason ||
                                        'The system is running, but no run has processed an outcome within the bound set below.'}{' '}
                                    It is still running, so this is not silence — check whether
                                    work stopped arriving upstream.
                                </p>
                            </div>
                        </div>
                    )}

                    <QuietProcessedBound
                        // A saved bound re-seeds the form from the server.
                        key={status.expectation.max_quiet_processed_seconds ?? 'none'}
                        seconds={status.expectation.max_quiet_processed_seconds ?? null}
                        busy={busy}
                        onSave={(seconds) => void onBoundChange(seconds)}
                    />

                    {/* Detector freshness. Never-checked is not health
                        information, and showing it as healthy would be the
                        exact failure this feature exists to prevent. */}
                    {!status.expectation.last_checked_at && status.configured && (
                        <p className="text-xs text-amber-700" data-testid="never-checked">
                            The detector has not evaluated this system yet — this is not a
                            health verdict.
                        </p>
                    )}

                    {/* Learned cadence */}
                    <div className="pt-2 border-t border-default">
                        {proposal ? (
                            <div data-testid="cadence-proposal" className="space-y-2">
                                <p className="text-sm text-default font-medium">
                                    Suggested cadence
                                </p>
                                <p className="text-xs text-muted">{proposal.rationale}</p>
                                {proposal.confident ? (
                                    <>
                                        <div className="grid grid-cols-3 gap-3">
                                            <Fact
                                                label="Window"
                                                value={humanizeSeconds(proposal.window_seconds)}
                                            />
                                            <Fact
                                                label="Grace"
                                                value={humanizeSeconds(proposal.grace_seconds)}
                                            />
                                            <Fact
                                                label="Min runs"
                                                value={String(proposal.expected_min_runs)}
                                            />
                                        </div>
                                        <button
                                            type="button"
                                            onClick={() => void onAccept()}
                                            disabled={busy}
                                            className="px-3 py-2 text-sm bg-primary-600 text-white rounded-lg hover:bg-primary-700 disabled:opacity-50"
                                        >
                                            Use this cadence
                                        </button>
                                    </>
                                ) : (
                                    /* Refusing to offer a one-click accept is the
                                       point: an expectation invented from three runs
                                       will fire, and the operator will stop trusting
                                       the whole category. */
                                    <p
                                        className="text-xs text-amber-700"
                                        data-testid="not-confident"
                                    >
                                        Not enough history to suggest a cadence yet. Set the
                                        window manually if you already know it.
                                    </p>
                                )}
                            </div>
                        ) : (
                            <button
                                type="button"
                                onClick={() => void onLearn()}
                                disabled={busy}
                                className="flex items-center gap-2 px-3 py-1.5 text-sm bg-primary-600 hover:bg-primary-700 text-white rounded-lg disabled:opacity-50"
                                data-testid="learn-cadence"
                            >
                                <Sparkles className="w-4 h-4" />
                                Suggest a cadence from observed runs
                            </button>
                        )}

                        {status.expectation.accepted_from_learned && !proposal && (
                            <p className="text-xs text-muted mt-2" data-testid="learned-badge">
                                Learned from {status.expectation.learned_from_runs} runs
                                {status.expectation.learned_p95_gap_seconds
                                    ? ` (95th-percentile gap ${humanizeSeconds(
                                          status.expectation.learned_p95_gap_seconds
                                      )})`
                                    : ''}
                                .
                            </p>
                        )}
                    </div>
                </>
            )}
        </div>
    );
}

/** The endpoint's ONE verdict with its reason underneath —
 *  through the shared mapping, never re-derived from the expectation. */
function StatusPill({ status }: { status: LivenessStatus }) {
    return (
        <div className="flex flex-col items-center gap-1 text-center">
            <LivenessBadge
                state={status.state}
                reason={status.reason}
                testId={`pill-${status.state}`}
                className="px-2.5 py-1 text-xs"
            />
            {status.reason && (
                <span className="max-w-xs text-[11px] text-muted" data-testid="liveness-reason">
                    {status.reason}
                </span>
            )}
        </div>
    );
}

function Fact({ label, value, help, testId }: { label: string; value: string; help?: string; testId?: string }) {
    return (
        <div data-testid={testId}>
            <div className="flex items-center gap-1 text-xs text-muted uppercase tracking-wider">
                {label}
                {help && <HelpTip title={label} text={help} className="normal-case tracking-normal" />}
            </div>
            <div className="text-sm text-default mt-0.5">{value}</div>
        </div>
    );
}

/** The optional bound on how long the system may run without processing
 *  an outcome — set for a scheduled system whose work should keep arriving.
 *  Empty clears it. Re-mounted (keyed) when the saved bound changes. */
function QuietProcessedBound({
    seconds,
    busy,
    onSave,
}: {
    seconds: number | null;
    busy: boolean;
    onSave: (seconds: number | null) => void;
}) {
    const initial = quietBoundParts(seconds);
    const [value, setValue] = useState(initial.value);
    const [unit, setUnit] = useState<QuietUnit>(initial.unit);
    const parsed = parseQuietBound(value, unit);
    const changed = !parsed.error && parsed.seconds !== seconds;
    return (
        <div data-testid="liveness-quiet-bound">
            <label
                htmlFor="liveness-quiet-bound-value"
                className="flex items-center gap-1.5 text-xs text-muted uppercase tracking-wider mb-1"
            >
                Alert when no work is processed for
                <HelpTip
                    title="No processed run for"
                    text="Optional. When the system keeps running but no run completes in a declared outcome that counts toward the baseline for this long — only empty polls, say — the system reads Work quiet. It is its own signal, never Silent: the agent is alive, the work is not arriving. Leave empty to not check. Between 1 minute and 366 days."
                    className="normal-case tracking-normal"
                />
            </label>
            <div className="flex items-center gap-2">
                <input
                    id="liveness-quiet-bound-value"
                    type="text"
                    inputMode="decimal"
                    value={value}
                    disabled={busy}
                    onChange={(e) => setValue(e.target.value)}
                    placeholder="not checked"
                    aria-invalid={Boolean(parsed.error)}
                    className="w-28 px-2 py-1.5 border border-strong rounded-lg bg-surface text-sm"
                    data-testid="liveness-quiet-bound-value"
                />
                <select
                    aria-label="Unit"
                    value={unit}
                    disabled={busy}
                    onChange={(e) => setUnit(e.target.value as QuietUnit)}
                    className="px-2 py-1.5 border border-strong rounded-lg bg-surface text-sm"
                    data-testid="liveness-quiet-bound-unit"
                >
                    {(Object.keys(QUIET_UNITS) as QuietUnit[]).map((u) => (
                        <option key={u} value={u}>
                            {u}
                        </option>
                    ))}
                </select>
                <button
                    type="button"
                    disabled={busy || !changed}
                    onClick={() => onSave(parsed.seconds)}
                    className="px-3 py-1.5 text-sm border border-strong rounded-lg hover:bg-surface-hover disabled:opacity-50"
                    data-testid="liveness-quiet-bound-save"
                >
                    {parsed.seconds === null && seconds !== null ? 'Clear' : 'Save'}
                </button>
            </div>
            {parsed.error && (
                <p className="text-xs text-red-700 mt-1" data-testid="liveness-quiet-bound-error">
                    {parsed.error}
                </p>
            )}
        </div>
    );
}

export default LivenessCard;

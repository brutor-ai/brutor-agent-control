/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The assurance report's "Excluded from judgement" section
 * (report spec 1.8, additive).
 *
 * Runs inside a declared incident window are recorded and sealed evidence;
 * no verdict rests on them. The report lists every window (who declared
 * it, why, how many runs it excluded, its sealed record) and every baseline
 * epoch, and coverage states the excluded period — unmeasured is not 100 %.
 * Spec 1.10 adds the approval holds raised inside the windows, which the
 * oversight rate does not judge.
 * The same words as the printed report (`lib/driftResolution`).
 */

import { HelpTip } from '../ui/HelpTip';
import { SignalBasis, driftMetricLabel } from '../../lib/aiSystemTypes';
import {
    epochText,
    excludedHoldsText,
    incidentCauseLabel,
    sealText,
    windowDeclaredText,
    windowRangeText,
    windowScopeText,
    type ExcludedPeriod,
    type JudgementExclusions,
} from '../../lib/driftResolution';

const basisOf = (v: string): SignalBasis | undefined => (v === 'runs' || v === 'segments' ? v : undefined);

export function JudgementExclusionsSection({
    exclusions,
    excludedPeriod,
}: {
    exclusions: JudgementExclusions | null;
    excludedPeriod: ExcludedPeriod | null;
}) {
    if (!exclusions && !excludedPeriod) return null;
    const windows = exclusions?.incident_windows ?? [];
    const epochs = exclusions?.baseline_epochs ?? [];
    const holds = excludedHoldsText(exclusions?.excluded_approval_requests);
    return (
        <div
            className="bg-surface border border-default rounded-lg p-4 space-y-3"
            data-testid="report-judgement-exclusions"
        >
            <div className="flex items-start gap-2">
                <p className="text-sm font-medium text-default">Excluded from judgement</p>
                <HelpTip text="Declared incident windows and baseline epochs in force. Runs inside a window are recorded, sealed and exportable, but no verdict in this report rests on them." />
            </div>

            {excludedPeriod && (
                <p
                    className={`text-sm ${excludedPeriod.incident_windows > 0 ? 'text-amber-900' : 'text-secondary'}`}
                    data-testid="report-excluded-period"
                >
                    {excludedPeriod.statement}
                </p>
            )}

            {holds && (
                <p className="text-sm text-secondary" data-testid="report-excluded-holds">
                    {holds}
                </p>
            )}

            {windows.length > 0 && (
                <div>
                    <p className="text-xs font-medium text-secondary mb-1">Incident windows</p>
                    <ul className="space-y-2" data-testid="report-incident-windows">
                        {windows.map((w) => (
                            <li key={w.id} className="text-xs border-l-2 border-strong pl-3" data-testid={`report-iw-${w.id}`}>
                                <p className="text-default">
                                    <span className="font-medium">{windowRangeText(w)}</span>
                                    {' · '}
                                    {incidentCauseLabel(w.cause_category)}
                                    {' · '}
                                    {windowScopeText(w)}
                                </p>
                                <p className="text-secondary">{w.reason}</p>
                                <p className="text-muted">
                                    {windowDeclaredText(w)} · {w.runs_excluded} run(s) excluded · {sealText(w.record_id)}
                                </p>
                            </li>
                        ))}
                    </ul>
                </div>
            )}

            {epochs.length > 0 && (
                <div>
                    <p className="text-xs font-medium text-secondary mb-1">Baseline epochs</p>
                    <ul className="space-y-1" data-testid="report-baseline-epochs">
                        {epochs.map((e) => (
                            <li key={e.id} className="text-xs" data-testid={`report-epoch-${e.id}`}>
                                <span className="text-default">{driftMetricLabel(e.metric_key, basisOf(e.basis))}</span>
                                <span className="text-secondary"> — {epochText(e)}</span>
                                {e.reason && <span className="text-muted"> · “{e.reason}”</span>}
                                <span className="text-muted"> · {sealText(e.record_id)}</span>
                            </li>
                        ))}
                    </ul>
                </div>
            )}
        </div>
    );
}

export default JudgementExclusionsSection;

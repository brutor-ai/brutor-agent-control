/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * How a run's declared outcome, run class and error cause read — one
 * rendering for the run list, run details and the Signals tables.
 *
 * Three questions, three marks: the error cause answers "did it complete
 * without errors"; the conformance chip "was the outcome one the contract
 * declares"; the baseline marker "does that outcome count as work". The run
 * class combines them (the control plane's call, never re-derived here).
 */

import type { OutcomeConformance, RunSummary } from '../../lib/aiSystemTypes';
import {
    CONFORMANCE_CLASSES,
    CONFORMANCE_HELP,
    CONFORMANCE_LABELS,
    ERROR_CAUSE_HELP,
    RUN_CLASS_CLASSES,
    RUN_CLASS_HELP,
    RUN_CLASS_LABELS,
    errorCauseLabel,
    isErrorCause,
    isRunClass,
} from '../../lib/runOutcomes';

const CHIP = 'inline-flex items-center px-1.5 py-0.5 rounded text-[11px] font-medium whitespace-nowrap';

/** `cls` is a string, not `RunClass`: a class this build does not know is
 *  shown verbatim rather than dropped. */
export function RunClassChip({ cls, testId }: { cls: string | null | undefined; testId?: string }) {
    if (!cls) return <span className="text-xs text-muted" data-testid={testId}>—</span>;
    const known = isRunClass(cls);
    return (
        <span
            className={`${CHIP} ${known ? RUN_CLASS_CLASSES[cls] : 'bg-surface-alt text-muted'}`}
            title={known ? RUN_CLASS_HELP[cls] : undefined}
            data-testid={testId}
            data-class={cls}
        >
            {known ? RUN_CLASS_LABELS[cls] : cls}
        </span>
    );
}

export function ConformanceChip({ conformance, testId }: { conformance: OutcomeConformance | null | undefined; testId?: string }) {
    if (!conformance) return null;
    return (
        <span className={`${CHIP} ${CONFORMANCE_CLASSES[conformance]}`} title={CONFORMANCE_HELP[conformance]} data-testid={testId}>
            {CONFORMANCE_LABELS[conformance]}
        </span>
    );
}

/** Whether a declared outcome counts toward the baseline: "no work" in the
 *  neutral grey (an empty poll ended as designed), "counts" otherwise. */
export function BaselineMarker({ counts, testId }: { counts: boolean | null | undefined; testId?: string }) {
    if (counts === null || counts === undefined) return null;
    return counts ? (
        <span
            className="text-[11px] text-muted"
            title="This declared outcome counts toward the operating baseline: it is the work."
            data-testid={testId}
        >
            counts
        </span>
    ) : (
        <span
            className={`${CHIP} bg-slate-100 text-slate-700`}
            title="This declared outcome does not count toward the operating baseline (for example nothing to do): no-work overhead, never a success."
            data-testid={testId}
        >
            no work
        </span>
    );
}

/** The outcome column: the id the agent reported (verbatim), its
 *  conformance, and the baseline marker. */
export function RunOutcomeCell({
    run,
}: {
    run: Pick<RunSummary, 'id' | 'outcome_id' | 'outcome_conformance' | 'outcome_counts_toward_baseline'>;
}) {
    return (
        <span className="flex flex-wrap items-center gap-1 min-w-0" data-testid={`outcome-${run.id}`}>
            {run.outcome_id ? (
                <code className="truncate font-mono text-xs text-default" title={run.outcome_id}>
                    {run.outcome_id}
                </code>
            ) : (
                run.outcome_conformance && <span className="text-xs italic text-muted">none</span>
            )}
            <ConformanceChip conformance={run.outcome_conformance} testId={`outcome-conformance-${run.id}`} />
            {run.outcome_conformance === 'declared' && (
                <BaselineMarker counts={run.outcome_counts_toward_baseline} testId={`outcome-baseline-${run.id}`} />
            )}
            {!run.outcome_id && !run.outcome_conformance && <span className="text-xs text-muted">—</span>}
        </span>
    );
}

/** An error cause in words, with what it means on hover. */
export function ErrorCauseText({ cause, testId }: { cause: string | null | undefined; testId?: string }) {
    if (!cause) return <span className="text-xs text-muted" data-testid={testId}>—</span>;
    return (
        <span
            className="text-xs text-red-700 dark:text-red-300"
            title={isErrorCause(cause) ? ERROR_CAUSE_HELP[cause] : undefined}
            data-testid={testId}
        >
            {errorCauseLabel(cause)}
        </span>
    );
}

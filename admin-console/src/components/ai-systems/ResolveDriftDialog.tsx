/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Close a drift finding.
 *
 * Closing is a decision the detector must honour, so the dialog asks for
 * WHICH fact it is and says, before the operator commits, what that fact
 * does to detection:
 *
 *   accepted change → the metric's baseline re-learns from the change point;
 *   cause fixed     → an incident window (proposed: change point → now,
 *                     editable) whose runs are kept but not judged;
 *   false positive  → the evidence up to this detection is not read again.
 *
 * There is no default kind and no close without a note: the platform
 * decides nothing silently. "Investigating" (acknowledge) is not here — it
 * never closes a finding and stays a separate action on the card.
 */

import React, { useMemo, useState } from 'react';
import { CheckCircle2, Loader, X } from 'lucide-react';

import { asyncHandler } from '../../lib/asyncSafe';
import type { DriftEvent } from '../../lib/aiSystemTypes';
import {
    HUMAN_RESOLUTION_KINDS,
    INCIDENT_CAUSE_CATEGORIES,
    INCIDENT_CAUSE_LABELS,
    RESOLUTION_KIND_EFFECTS,
    RESOLUTION_KIND_LABELS,
    RESOLUTION_KIND_MEANING,
    type CloseDriftRequest,
    type IncidentCauseCategory,
} from '../../lib/driftResolutionTypes';
import { closeRequestOf, proposedWindow, toLocalInputValue, type CloseDraft } from '../../lib/driftResolution';
import { errorMessage } from '../../lib/narrow';

interface ResolveDriftDialogProps {
    /** The finding(s) being closed — a grouped card closes every member. */
    events: DriftEvent[];
    /** What the finding is, in words (the card's title). */
    title: string;
    onConfirm: (request: CloseDriftRequest) => Promise<void>;
    onCancel: () => void;
    /** Injected for tests; the proposed window ends now. */
    now?: Date;
}

const INPUT =
    'w-full rounded-md border border-strong bg-surface px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500';

export const ResolveDriftDialog: React.FC<ResolveDriftDialogProps> = ({ events, title, onConfirm, onCancel, now }) => {
    const proposal = useMemo(() => proposedWindow(events, now ?? new Date()), [events, now]);
    const [draft, setDraft] = useState<CloseDraft>({
        kind: null,
        note: '',
        windowStart: toLocalInputValue(proposal.startsAt),
        windowEnd: toLocalInputValue(proposal.endsAt),
        causeCategory: 'other',
        tenantWide: false,
    });
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const outcome = closeRequestOf(draft);
    const blocked = 'error' in outcome ? outcome.error : null;
    const set = <K extends keyof CloseDraft>(key: K, value: CloseDraft[K]) =>
        setDraft((d) => ({ ...d, [key]: value }));

    const submit = async () => {
        if (!('request' in outcome) || submitting) return;
        setSubmitting(true);
        setError(null);
        try {
            await onConfirm(outcome.request);
        } catch (e) {
            setError(errorMessage(e, 'Could not close the finding'));
            setSubmitting(false);
        }
    };

    return (
        <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
            role="dialog"
            aria-modal="true"
            aria-labelledby="resolve-drift-title"
            data-testid="resolve-drift-dialog"
        >
            <div className="w-full max-w-xl max-h-[90vh] overflow-y-auto rounded-lg border border-default bg-surface shadow-xl">
                <div className="flex items-start justify-between border-b border-default p-4">
                    <div className="flex items-start gap-3">
                        <CheckCircle2 className="mt-0.5 h-5 w-5 flex-shrink-0 text-primary-600" />
                        <div>
                            <h2 id="resolve-drift-title" className="text-base font-medium text-default">
                                Close this finding
                            </h2>
                            <p className="mt-0.5 text-xs text-muted">
                                {title}
                                {events.length > 1 && ` · applies to all ${events.length} grouped findings`}
                            </p>
                        </div>
                    </div>
                    <button
                        type="button"
                        onClick={onCancel}
                        className="rounded p-1 text-muted hover:bg-surface-alt"
                        aria-label="Cancel"
                    >
                        <X className="h-4 w-4" />
                    </button>
                </div>

                <div className="space-y-4 p-4">
                    <fieldset>
                        <legend className="mb-2 text-sm font-medium text-secondary">
                            What is it? <span className="text-red-600">*</span>
                        </legend>
                        <div className="space-y-2" role="radiogroup">
                            {HUMAN_RESOLUTION_KINDS.map((kind) => {
                                const selected = draft.kind === kind;
                                return (
                                    <label
                                        key={kind}
                                        className={`flex cursor-pointer items-start gap-3 rounded-lg border p-3 ${
                                            selected ? 'border-primary-500 bg-primary-50' : 'border-default hover:bg-surface-hover'
                                        }`}
                                        data-testid={`resolve-kind-${kind}`}
                                    >
                                        <input
                                            type="radio"
                                            name="resolution-kind"
                                            value={kind}
                                            checked={selected}
                                            onChange={() => set('kind', kind)}
                                            className="mt-1"
                                        />
                                        <span className="min-w-0">
                                            <span className="block text-sm font-medium text-default">
                                                {RESOLUTION_KIND_LABELS[kind]}
                                            </span>
                                            <span className="block text-xs text-secondary">
                                                {RESOLUTION_KIND_MEANING[kind]}
                                            </span>
                                            <span
                                                className="mt-1 block text-xs text-muted"
                                                data-testid={`resolve-effect-${kind}`}
                                            >
                                                <span className="font-medium">Effect on detection:</span>{' '}
                                                {RESOLUTION_KIND_EFFECTS[kind]}
                                            </span>
                                        </span>
                                    </label>
                                );
                            })}
                        </div>
                    </fieldset>

                    {draft.kind === 'cause_fixed' && (
                        <fieldset
                            className="space-y-3 rounded-lg border border-default bg-surface-alt p-3"
                            data-testid="resolve-window"
                        >
                            <legend className="px-1 text-sm font-medium text-secondary">Incident window</legend>
                            <p className="text-xs text-muted">
                                Proposed from when the behaviour moved to now. Runs that started inside
                                it stay recorded and sealed, but are not judged.
                            </p>
                            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                                <div>
                                    <label htmlFor="resolve-window-start" className="mb-1 block text-xs font-medium text-secondary">
                                        Start
                                    </label>
                                    <input
                                        id="resolve-window-start"
                                        type="datetime-local"
                                        value={draft.windowStart}
                                        onChange={(e) => set('windowStart', e.target.value)}
                                        className={INPUT}
                                        data-testid="resolve-window-start"
                                    />
                                </div>
                                <div>
                                    <label htmlFor="resolve-window-end" className="mb-1 block text-xs font-medium text-secondary">
                                        End (the fix)
                                    </label>
                                    <input
                                        id="resolve-window-end"
                                        type="datetime-local"
                                        value={draft.windowEnd}
                                        onChange={(e) => set('windowEnd', e.target.value)}
                                        className={INPUT}
                                        data-testid="resolve-window-end"
                                    />
                                </div>
                            </div>
                            <div>
                                <label htmlFor="resolve-cause-category" className="mb-1 block text-xs font-medium text-secondary">
                                    Cause category
                                </label>
                                <select
                                    id="resolve-cause-category"
                                    value={draft.causeCategory}
                                    onChange={(e) => set('causeCategory', e.target.value as IncidentCauseCategory)}
                                    className={INPUT}
                                    data-testid="resolve-cause-category"
                                >
                                    {INCIDENT_CAUSE_CATEGORIES.map((c) => (
                                        <option key={c} value={c}>
                                            {INCIDENT_CAUSE_LABELS[c]}
                                        </option>
                                    ))}
                                </select>
                            </div>
                            <label className="flex items-start gap-2 text-xs text-secondary">
                                <input
                                    type="checkbox"
                                    checked={draft.tenantWide}
                                    onChange={(e) => set('tenantWide', e.target.checked)}
                                    className="mt-0.5"
                                    data-testid="resolve-tenant-wide"
                                />
                                <span>
                                    Every AI System in the tenant — a provider or platform outage, not
                                    just this system.
                                </span>
                            </label>
                        </fieldset>
                    )}

                    <div>
                        <label htmlFor="resolve-note" className="mb-1 block text-sm font-medium text-secondary">
                            Note <span className="text-red-600">*</span>
                        </label>
                        <textarea
                            id="resolve-note"
                            value={draft.note}
                            onChange={(e) => set('note', e.target.value)}
                            rows={3}
                            maxLength={2000}
                            placeholder="e.g. provider credits ran out at 21:00; restored 06:40"
                            data-testid="resolve-note"
                            className={INPUT}
                        />
                        <p className="mt-1 text-xs text-muted">
                            Sealed with the resolution as a declaration.
                        </p>
                    </div>

                    {error && (
                        <p className="text-sm text-red-600" data-testid="resolve-error">
                            {error}
                        </p>
                    )}
                </div>

                <div className="flex items-center justify-end gap-2 border-t border-default p-4">
                    {blocked && (
                        <p className="mr-auto text-xs text-muted" data-testid="resolve-blocked">
                            {blocked}
                        </p>
                    )}
                    <button
                        type="button"
                        onClick={onCancel}
                        disabled={submitting}
                        className="rounded-lg border border-strong px-4 py-2 text-sm font-medium text-secondary hover:bg-surface-alt disabled:opacity-50"
                    >
                        Cancel
                    </button>
                    <button
                        type="button"
                        onClick={asyncHandler(submit, 'click')}
                        disabled={Boolean(blocked) || submitting}
                        data-testid="resolve-confirm"
                        className="flex items-center gap-2 rounded-lg bg-primary-600 px-4 py-2 text-sm font-medium text-white hover:bg-primary-700 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                        {submitting && <Loader className="h-4 w-4 animate-spin" />}
                        Close finding
                    </button>
                </div>
            </div>
        </div>
    );
};

export default ResolveDriftDialog;

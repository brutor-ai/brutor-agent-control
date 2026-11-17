/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Stop one in-flight run.
 *
 * Two things this dialog must not do.
 *
 * It must not let the operator stop a run without saying why. An
 * intervention with no stated reason is not evidence, and "why did this run
 * stop?" is the first question asked afterwards — usually by someone who was
 * not in the room.
 *
 * And it must not overclaim. Abort stops the run's NEXT governed action; a
 * provider call already in flight completes. Saying so plainly here is the
 * difference between a control an operator can reason about during an
 * incident and one they discover the limits of during it.
 */

import React, { useState } from 'react';
import { AlertTriangle, Loader, X } from 'lucide-react';
import { asyncHandler } from '../../lib/asyncSafe';

interface AbortRunDialogProps {
    rootTaskId: string;
    /** Shown so the operator can confirm they are stopping the right run. */
    aiSystemName?: string | null;
    onConfirm: (reason: string) => Promise<void>;
    onCancel: () => void;
}

export const AbortRunDialog: React.FC<AbortRunDialogProps> = ({
    rootTaskId,
    aiSystemName,
    onConfirm,
    onCancel,
}) => {
    const [reason, setReason] = useState('');
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const trimmed = reason.trim();
    const canSubmit = trimmed.length > 0 && !submitting;

    const submit = async () => {
        if (!canSubmit) return;
        setSubmitting(true);
        setError(null);
        try {
            await onConfirm(trimmed);
        } catch (e) {
            setError(e instanceof Error ? e.message : 'Failed to stop the run');
            setSubmitting(false);
        }
    };

    return (
        <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
            role="dialog"
            aria-modal="true"
            aria-labelledby="abort-run-title"
            data-testid="abort-run-dialog"
        >
            <div className="w-full max-w-lg rounded-lg border border-default bg-surface shadow-xl">
                <div className="flex items-start justify-between border-b border-default p-4">
                    <div className="flex items-start gap-3">
                        <AlertTriangle className="mt-0.5 h-5 w-5 flex-shrink-0 text-amber-600" />
                        <div>
                            <h2 id="abort-run-title" className="text-base font-medium text-default">
                                Stop this run
                            </h2>
                            <p className="mt-0.5 text-xs text-muted">
                                {aiSystemName ? `${aiSystemName} · ` : ''}
                                <code className="font-mono">{rootTaskId}</code>
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
                    <p className="text-sm text-secondary" data-testid="abort-scope-note">
                        This stops <strong>only this run</strong>. Other runs of the same AI
                        System keep working.
                    </p>

                    {/* The honest limit, stated where the decision is made. */}
                    <p
                        className="rounded-md border border-default bg-surface-alt p-3 text-xs text-secondary"
                        data-testid="abort-limit-note"
                    >
                        The run is refused at its <strong>next</strong> governed action. A model
                        or tool call already in flight completes — stopping a run does not undo
                        what it has already done.
                    </p>

                    <div>
                        <label
                            htmlFor="abort-reason"
                            className="mb-1 block text-sm font-medium text-secondary"
                        >
                            Reason <span className="text-red-600">*</span>
                        </label>
                        <textarea
                            id="abort-reason"
                            value={reason}
                            onChange={(e) => setReason(e.target.value)}
                            rows={3}
                            maxLength={1024}
                            autoFocus
                            placeholder="e.g. looping on the same tool call since 14:02"
                            data-testid="abort-reason"
                            className="w-full rounded-md border border-strong bg-surface px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500"
                        />
                        <p className="mt-1 text-xs text-muted">
                            Recorded as evidence on the run and in the audit trail.
                        </p>
                    </div>

                    {error && (
                        <p className="text-sm text-red-600" data-testid="abort-error">
                            {error}
                        </p>
                    )}
                </div>

                <div className="flex justify-end gap-2 border-t border-default p-4">
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
                        disabled={!canSubmit}
                        data-testid="abort-confirm"
                        title={trimmed ? undefined : 'A reason is required'}
                        className="flex items-center gap-2 rounded-lg bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                        {submitting && <Loader className="h-4 w-4 animate-spin" />}
                        Stop run
                    </button>
                </div>
            </div>
        </div>
    );
};

export default AbortRunDialog;

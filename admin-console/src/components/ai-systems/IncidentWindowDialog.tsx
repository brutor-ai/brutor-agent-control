/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Declare or edit an incident window.
 *
 * A window is a declared period whose runs are excluded from judgement but
 * kept as evidence. Declaring and editing are each a sealed declaration, so
 * a reason is required. The scope (one system / every system in the tenant)
 * is chosen on declaring and is not editable afterwards — withdraw and
 * declare again — which is the control plane's rule, stated here.
 */

import React, { useState } from 'react';
import { CalendarX, Loader, X } from 'lucide-react';

import { asyncHandler } from '../../lib/asyncSafe';
import {
    INCIDENT_CAUSE_CATEGORIES,
    INCIDENT_CAUSE_LABELS,
    INCIDENT_RESOURCE_LABELS,
    INCIDENT_RESOURCE_TYPES,
    type IncidentCauseCategory,
    type IncidentResourceType,
    type IncidentWindow,
    type IncidentWindowCreate,
    type IncidentWindowUpdate,
} from '../../lib/driftResolutionTypes';
import { fromLocalInputValue, toLocalInputValue, windowScopeText } from '../../lib/driftResolution';
import { errorMessage } from '../../lib/narrow';

/** The system scope choice on the tenant page: '' = every system. */
export interface SystemOption {
    id: string;
    name: string;
}

interface IncidentWindowDialogProps {
    /** Edit this window; omitted = declare a new one. */
    window?: IncidentWindow | null;
    /** Fixed scope (the AI System page). Omitted on the tenant page, which
     *  offers `systems` and "every AI System in the tenant". */
    aiSystemId?: string;
    systems?: SystemOption[];
    onCreate: (body: IncidentWindowCreate) => Promise<void>;
    onUpdate: (id: string, body: IncidentWindowUpdate) => Promise<void>;
    onCancel: () => void;
}

const INPUT =
    'w-full rounded-md border border-strong bg-surface px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500';

export const IncidentWindowDialog: React.FC<IncidentWindowDialogProps> = ({
    window: editing,
    aiSystemId,
    systems = [],
    onCreate,
    onUpdate,
    onCancel,
}) => {
    const [start, setStart] = useState(toLocalInputValue(editing?.starts_at));
    const [end, setEnd] = useState(toLocalInputValue(editing?.ends_at));
    const [category, setCategory] = useState<IncidentCauseCategory>(editing?.cause_category ?? 'maintenance');
    const [reason, setReason] = useState(editing?.reason ?? '');
    const [resourceType, setResourceType] = useState<IncidentResourceType | ''>(editing?.resource_type ?? '');
    const [resourceId, setResourceId] = useState(editing?.resource_id ?? '');
    const [scope, setScope] = useState<string>(aiSystemId ?? '');
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const startsAt = fromLocalInputValue(start);
    const endsAt = fromLocalInputValue(end);
    const blocked = !startsAt || !endsAt
        ? 'A start and an end are required.'
        : new Date(endsAt).getTime() <= new Date(startsAt).getTime()
          ? 'The window must end after it starts.'
          : !reason.trim()
            ? 'A reason is required.'
            : resourceType && !resourceId.trim()
              ? 'A resource needs its id (type and id go together).'
              : null;

    const submit = async () => {
        if (blocked || !startsAt || !endsAt || submitting) return;
        setSubmitting(true);
        setError(null);
        const resource = {
            resource_type: resourceType || null,
            resource_id: resourceType ? resourceId.trim() : null,
        };
        try {
            if (editing) {
                await onUpdate(editing.id, {
                    starts_at: startsAt,
                    ends_at: endsAt,
                    cause_category: category,
                    reason: reason.trim(),
                    ...resource,
                });
            } else {
                await onCreate({
                    ai_system_id: scope || null,
                    starts_at: startsAt,
                    ends_at: endsAt,
                    cause_category: category,
                    reason: reason.trim(),
                    ...resource,
                });
            }
        } catch (e) {
            setError(errorMessage(e, 'Could not save the incident window'));
            setSubmitting(false);
        }
    };

    return (
        <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
            role="dialog"
            aria-modal="true"
            aria-labelledby="incident-window-title"
            data-testid="incident-window-dialog"
        >
            <div className="w-full max-w-lg max-h-[90vh] overflow-y-auto rounded-lg border border-default bg-surface shadow-xl">
                <div className="flex items-start justify-between border-b border-default p-4">
                    <div className="flex items-start gap-3">
                        <CalendarX className="mt-0.5 h-5 w-5 flex-shrink-0 text-secondary" />
                        <div>
                            <h2 id="incident-window-title" className="text-base font-medium text-default">
                                {editing ? 'Edit incident window' : 'Declare an incident window'}
                            </h2>
                            <p className="mt-0.5 text-xs text-muted">
                                Runs that start inside it are recorded and sealed, but not judged.
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
                    <div>
                        <label htmlFor="iw-scope" className="mb-1 block text-xs font-medium text-secondary">
                            Applies to
                        </label>
                        {editing ? (
                            <p className="text-sm text-default" data-testid="iw-scope-fixed">
                                {windowScopeText(editing)}{' '}
                                <span className="text-xs text-muted">(withdraw and declare again to change)</span>
                            </p>
                        ) : aiSystemId ? (
                            <p className="text-sm text-default" data-testid="iw-scope-fixed">This AI System</p>
                        ) : (
                            <select
                                id="iw-scope"
                                value={scope}
                                onChange={(e) => setScope(e.target.value)}
                                className={INPUT}
                                data-testid="iw-scope"
                            >
                                <option value="">Every AI System in the tenant</option>
                                {systems.map((s) => (
                                    <option key={s.id} value={s.id}>
                                        {s.name}
                                    </option>
                                ))}
                            </select>
                        )}
                    </div>

                    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                        <div>
                            <label htmlFor="iw-start" className="mb-1 block text-xs font-medium text-secondary">
                                Start
                            </label>
                            <input
                                id="iw-start"
                                type="datetime-local"
                                value={start}
                                onChange={(e) => setStart(e.target.value)}
                                className={INPUT}
                                data-testid="iw-start"
                            />
                        </div>
                        <div>
                            <label htmlFor="iw-end" className="mb-1 block text-xs font-medium text-secondary">
                                End (exclusive)
                            </label>
                            <input
                                id="iw-end"
                                type="datetime-local"
                                value={end}
                                onChange={(e) => setEnd(e.target.value)}
                                className={INPUT}
                                data-testid="iw-end"
                            />
                        </div>
                    </div>

                    <div>
                        <label htmlFor="iw-category" className="mb-1 block text-xs font-medium text-secondary">
                            Cause category
                        </label>
                        <select
                            id="iw-category"
                            value={category}
                            onChange={(e) => setCategory(e.target.value as IncidentCauseCategory)}
                            className={INPUT}
                            data-testid="iw-category"
                        >
                            {INCIDENT_CAUSE_CATEGORIES.map((c) => (
                                <option key={c} value={c}>
                                    {INCIDENT_CAUSE_LABELS[c]}
                                </option>
                            ))}
                        </select>
                    </div>

                    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                        <div>
                            <label htmlFor="iw-resource-type" className="mb-1 block text-xs font-medium text-secondary">
                                Resource (optional, for display)
                            </label>
                            <select
                                id="iw-resource-type"
                                value={resourceType}
                                onChange={(e) => setResourceType(e.target.value as IncidentResourceType | '')}
                                className={INPUT}
                                data-testid="iw-resource-type"
                            >
                                <option value="">None</option>
                                {INCIDENT_RESOURCE_TYPES.map((t) => (
                                    <option key={t} value={t}>
                                        {INCIDENT_RESOURCE_LABELS[t]}
                                    </option>
                                ))}
                            </select>
                        </div>
                        {resourceType && (
                            <div>
                                <label htmlFor="iw-resource-id" className="mb-1 block text-xs font-medium text-secondary">
                                    Resource id
                                </label>
                                <input
                                    id="iw-resource-id"
                                    value={resourceId}
                                    onChange={(e) => setResourceId(e.target.value)}
                                    maxLength={255}
                                    className={INPUT}
                                    data-testid="iw-resource-id"
                                />
                            </div>
                        )}
                    </div>

                    <div>
                        <label htmlFor="iw-reason" className="mb-1 block text-sm font-medium text-secondary">
                            Reason <span className="text-red-600">*</span>
                        </label>
                        <textarea
                            id="iw-reason"
                            value={reason}
                            onChange={(e) => setReason(e.target.value)}
                            rows={3}
                            maxLength={2000}
                            placeholder="e.g. planned provider migration, 02:00–04:00"
                            className={INPUT}
                            data-testid="iw-reason"
                        />
                        <p className="mt-1 text-xs text-muted">Sealed as a declaration, with who made it.</p>
                    </div>

                    {error && (
                        <p className="text-sm text-red-600" data-testid="iw-error">
                            {error}
                        </p>
                    )}
                </div>

                <div className="flex items-center justify-end gap-2 border-t border-default p-4">
                    {blocked && (
                        <p className="mr-auto text-xs text-muted" data-testid="iw-blocked">
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
                        data-testid="iw-save"
                        className="flex items-center gap-2 rounded-lg bg-primary-600 px-4 py-2 text-sm font-medium text-white hover:bg-primary-700 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                        {submitting && <Loader className="h-4 w-4 animate-spin" />}
                        {editing ? 'Save' : 'Declare'}
                    </button>
                </div>
            </div>
        </div>
    );
};

export default IncidentWindowDialog;

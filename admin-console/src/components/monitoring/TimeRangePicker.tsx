/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * <TimeRangePicker /> — preset + custom time-range selector.
 *
 * Sits at the top of both log viewers (Proxy Logs, Admin Audit
 * Logs). Operates on a discriminated-union value so the parent can
 * decide whether to send `hours_ago` (preset) or
 * `start_time + end_time` (custom) to the backend, without losing
 * which choice the operator actually made when serialising state
 * to URL params later.
 *
 * Design choices that aren't obvious:
 *
 *   - Presets are minutes-aware: a "15 min" preset doesn't round up
 *     to 1 hour. The backend's `hours_ago` is float-typed (Pydantic
 *     ge=1) but we cap to 1 minimum, so for sub-hour ranges we
 *     emit explicit start/end times instead. Same data, cleaner
 *     server-side query plan.
 *
 *   - Custom range edits update the parent IMMEDIATELY (no Apply
 *     button) because the inputs are native datetime-local. That
 *     matches Datadog / Sentry; the alternative is friction every
 *     operator hates.
 *
 *   - The picker exposes its current label via the trigger button
 *     so the time range survives a collapsed top bar — operators
 *     scroll the table and the time-range chip stays in view at
 *     the toolbar level (matches Auth0).
 */

import React, { useEffect, useRef, useState } from 'react';
import { Clock, ChevronDown } from 'lucide-react';

export type TimeRange =
    | { kind: 'preset'; minutes: number }
    | { kind: 'custom'; start: string; end: string };

export interface TimeRangePickerProps {
    value: TimeRange;
    onChange: (next: TimeRange) => void;
}

interface Preset {
    label: string;
    minutes: number;
}

// The preset ladder is intentionally biased toward shorter ranges —
// audit-log operators land here in response to an alert / incident
// 80% of the time, not for historical analysis. The bigger ranges
// stay there for the 20% case but don't dominate the menu.
const PRESETS: Preset[] = [
    { label: 'Last 15 minutes', minutes: 15 },
    { label: 'Last 1 hour',     minutes: 60 },
    { label: 'Last 4 hours',    minutes: 240 },
    { label: 'Last 24 hours',   minutes: 60 * 24 },
    { label: 'Last 7 days',     minutes: 60 * 24 * 7 },
    { label: 'Last 30 days',    minutes: 60 * 24 * 30 },
];

/**
 * Convert a preset's minute count to the most operator-friendly
 * label that matches the menu entries above. Used for the trigger
 * button so the operator sees the same phrasing they picked.
 */
function presetLabel(minutes: number): string {
    const match = PRESETS.find(p => p.minutes === minutes);
    if (match) return match.label;
    if (minutes < 60) return `Last ${minutes} min`;
    if (minutes < 60 * 24) return `Last ${Math.round(minutes / 60)} hours`;
    return `Last ${Math.round(minutes / (60 * 24))} days`;
}

/**
 * Native datetime-local inputs use the local timezone; the wire
 * format is ISO-8601 UTC. These two helpers bridge the gap.
 */
function toLocalInputValue(iso: string): string {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    const pad = (n: number) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fromLocalInputValue(local: string): string {
    if (!local) return '';
    const d = new Date(local);
    if (Number.isNaN(d.getTime())) return '';
    return d.toISOString();
}

const TimeRangePicker: React.FC<TimeRangePickerProps> = ({ value, onChange }) => {
    const [open, setOpen] = useState(false);
    const ref = useRef<HTMLDivElement>(null);

    useEffect(() => {
        if (!open) return;
        const close = (e: MouseEvent) => {
            if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
        };
        document.addEventListener('mousedown', close);
        return () => document.removeEventListener('mousedown', close);
    }, [open]);

    const triggerLabel =
        value.kind === 'preset'
            ? presetLabel(value.minutes)
            : `${value.start.slice(0, 16).replace('T', ' ')} → ${value.end.slice(0, 16).replace('T', ' ')}`;

    const pickPreset = (minutes: number) => {
        onChange({ kind: 'preset', minutes });
        setOpen(false);
    };

    return (
        <div className="relative" ref={ref}>
            <button
                type="button"
                onClick={() => setOpen(o => !o)}
                className="flex items-center space-x-1.5 px-2 py-1 text-xs font-medium text-default border border-strong rounded bg-surface hover:bg-surface-alt"
                aria-expanded={open}
            >
                <Clock className="w-3.5 h-3.5 text-muted" />
                <span>{triggerLabel}</span>
                <ChevronDown className={`w-3.5 h-3.5 transition-transform ${open ? 'rotate-180' : ''}`} />
            </button>
            {open && (
                <div className="absolute z-50 mt-1 right-0 min-w-[280px] bg-surface border border-strong rounded-md shadow-lg overflow-hidden">
                    <div className="border-b border-default">
                        {PRESETS.map(p => (
                            <button
                                key={p.minutes}
                                type="button"
                                onClick={() => pickPreset(p.minutes)}
                                className={`w-full text-left px-3 py-1.5 text-xs hover:bg-surface-alt ${
                                    value.kind === 'preset' && value.minutes === p.minutes
                                        ? 'bg-primary-50 text-primary-700 font-medium'
                                        : 'text-default'
                                }`}
                            >
                                {p.label}
                            </button>
                        ))}
                    </div>
                    <div className="p-3 space-y-2">
                        <div className="text-[11px] font-semibold text-secondary uppercase tracking-wide">
                            Custom range
                        </div>
                        <div>
                            <label className="block text-[11px] text-muted mb-1">From</label>
                            <input
                                type="datetime-local"
                                value={
                                    value.kind === 'custom'
                                        ? toLocalInputValue(value.start)
                                        : ''
                                }
                                onChange={(e) => {
                                    const start = fromLocalInputValue(e.target.value);
                                    const end =
                                        value.kind === 'custom' && value.end
                                            ? value.end
                                            : new Date().toISOString();
                                    onChange({ kind: 'custom', start, end });
                                }}
                                className="w-full px-2 py-1 text-xs border border-strong rounded bg-surface"
                            />
                        </div>
                        <div>
                            <label className="block text-[11px] text-muted mb-1">To</label>
                            <input
                                type="datetime-local"
                                value={
                                    value.kind === 'custom'
                                        ? toLocalInputValue(value.end)
                                        : ''
                                }
                                onChange={(e) => {
                                    const end = fromLocalInputValue(e.target.value);
                                    const start =
                                        value.kind === 'custom' && value.start
                                            ? value.start
                                            : new Date(Date.now() - 60 * 60 * 1000).toISOString();
                                    onChange({ kind: 'custom', start, end });
                                }}
                                className="w-full px-2 py-1 text-xs border border-strong rounded bg-surface"
                            />
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
};

export default TimeRangePicker;

/**
 * Convert the discriminated TimeRange into the backend body shape:
 *   preset → { hours_ago: ceil(minutes/60) | undefined when <60 → start/end }
 *   custom → { start_time, end_time }
 *
 * Centralising this in one place means a future backend rename
 * (e.g. `minutes_ago`) is one find/replace, not five.
 */

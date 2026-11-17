/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The words and the arithmetic of drift resolution, pure so the
 * Drift tab, the incident-window lists, the charts, the report panel and
 * the printed report say exactly the same thing and the tests pin it.
 */

import type { DriftEvent, RunTimeseriesPoint } from './aiSystemTypes';
import {
    EPOCH_ALL_CALLERS,
    INCIDENT_CAUSE_CATEGORIES,
    INCIDENT_CAUSE_LABELS,
    RESOLUTION_KIND_LABELS,
    type BaselineEpoch,
    type CloseDriftRequest,
    type HumanResolutionKind,
    type IncidentCauseCategory,
    type IncidentWindow,
} from './driftResolutionTypes';
import { getArray, getBoolean, getNumber, getRecord, getString, isRecord } from './narrow';

// ---------------------------------------------------------------------------
// Time
// ---------------------------------------------------------------------------

export function formatWhen(iso?: string | null): string {
    if (!iso) return '—';
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/** "<start> → <end>" — an open end reads "still open", never blank. */
export function windowRangeText(w: { starts_at?: string | null; ends_at?: string | null }): string {
    return `${formatWhen(w.starts_at)} → ${w.ends_at ? formatWhen(w.ends_at) : 'still open'}`;
}

/** An ISO instant as a `<input type="datetime-local">` value (local time). */
export function toLocalInputValue(iso?: string | null): string {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    const pad = (n: number) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** A `datetime-local` value back to an ISO instant; null when unparseable. */
export function fromLocalInputValue(value: string): string | null {
    if (!value) return null;
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

// ---------------------------------------------------------------------------
// Closing a finding
// ---------------------------------------------------------------------------

/** The incident window a `cause_fixed` close proposes: from when the
 *  behaviour moved (the change point, else when it was detected) to now.
 *  Over a grouped card, from the earliest member's start. */
export function proposedWindow(
    events: Pick<DriftEvent, 'change_point_at' | 'detected_at'>[],
    now: Date = new Date(),
): { startsAt: string; endsAt: string } {
    const starts = events
        .map((e) => e.change_point_at ?? e.detected_at)
        .filter((s): s is string => Boolean(s))
        .map((s) => new Date(s).getTime())
        .filter((t) => !Number.isNaN(t));
    const start = starts.length ? Math.min(...starts) : now.getTime();
    return { startsAt: new Date(start).toISOString(), endsAt: now.toISOString() };
}

export interface CloseDraft {
    kind: HumanResolutionKind | null;
    note: string;
    /** `datetime-local` values — cause_fixed only. */
    windowStart: string;
    windowEnd: string;
    causeCategory: IncidentCauseCategory;
    tenantWide: boolean;
}

/** The request a draft sends, or the reason it cannot be sent yet. A kind
 *  and a note are always required; cause_fixed also needs a window whose
 *  end is after its start. Window fields are sent for cause_fixed ONLY —
 *  the control plane refuses them on the other kinds. */
export function closeRequestOf(draft: CloseDraft): { request: CloseDriftRequest } | { error: string } {
    if (!draft.kind) return { error: 'Choose what kind of resolution this is.' };
    const note = draft.note.trim();
    if (!note) return { error: 'A note is required.' };
    if (draft.kind !== 'cause_fixed') return { request: { kind: draft.kind, note } };
    const starts = fromLocalInputValue(draft.windowStart);
    const ends = fromLocalInputValue(draft.windowEnd);
    if (!starts || !ends) return { error: 'The incident window needs a start and an end.' };
    if (new Date(ends).getTime() <= new Date(starts).getTime()) {
        return { error: 'The incident window must end after it starts.' };
    }
    return {
        request: {
            kind: 'cause_fixed',
            note,
            window_starts_at: starts,
            window_ends_at: ends,
            cause_category: draft.causeCategory,
            tenant_wide: draft.tenantWide,
        },
    };
}

// ---------------------------------------------------------------------------
// Epochs
// ---------------------------------------------------------------------------

/** The epoch in force for a baseline: same metric and basis, caller the
 *  same or 'all', newest first — the control plane's `epoch_covers`. */
export function epochInForce(
    epochs: BaselineEpoch[],
    metricKey: string,
    basis?: string | null,
    callerSystemId?: string | null,
    bucket?: string | null,
): BaselineEpoch | null {
    const callers = new Set([EPOCH_ALL_CALLERS, callerSystemId || EPOCH_ALL_CALLERS]);
    // An epoch on bucket 'all' covers every bucket; one on an outcome
    // bucket (a catalogue change) covers only that bucket.
    const buckets = new Set([EPOCH_ALL_CALLERS, bucket || EPOCH_ALL_CALLERS]);
    const covering = epochs.filter(
        (e) =>
            e.metric_key === metricKey &&
            (e.basis || 'runs') === (basis || 'runs') &&
            callers.has(e.caller_system_id || EPOCH_ALL_CALLERS) &&
            buckets.has(e.bucket || EPOCH_ALL_CALLERS),
    );
    covering.sort((a, b) => (b.starts_at ?? '').localeCompare(a.starts_at ?? ''));
    return covering[0] ?? null;
}

/** "re-learning since … — accepted change by …" */
export function epochText(epoch: BaselineEpoch): string {
    const who = epoch.declared_by ?? 'an operator';
    switch (epoch.kind) {
        case 'accepted_change':
            return `re-learning since ${formatWhen(epoch.starts_at)} — accepted change by ${who}`;
        case 'measurement_change': {
            // Not a human decision and not the agent changing: the
            // platform changed how this metric is measured.
            const reason = epoch.reason?.trim();
            return `re-learning since ${formatWhen(epoch.starts_at)} — measurement changed by the platform${reason ? `: ${reason}` : ''}`;
        }
        case 'catalogue_change': {
            // A promoted contract changed what this outcome means (its
            // expected evidence, or whether it counts toward the baseline).
            const what = epoch.bucket && epoch.bucket !== EPOCH_ALL_CALLERS ? ` for outcome ${epoch.bucket}` : '';
            return `re-learning${what} since ${formatWhen(epoch.starts_at)} — the declared outcomes changed in a new contract`;
        }
        default:
            return `evidence before ${formatWhen(epoch.starts_at)} set aside — marked not drift by ${who}`;
    }
}

// ---------------------------------------------------------------------------
// A closed finding
// ---------------------------------------------------------------------------

/** What a closed finding's kind did to detection, read from the effect
 *  the control plane recorded (the epoch or window that carries the
 *  finding's id). */
export function closedEffect(
    event: Pick<DriftEvent, 'id' | 'status' | 'resolution_kind' | 'change_point_at' | 'detected_at'>,
    epochs: BaselineEpoch[],
    windows: IncidentWindow[],
): { label: string; effect: string | null } {
    const kind = event.resolution_kind;
    if (!kind) {
        // Closed without a resolution kind: no effect on detection.
        return {
            label: event.status === 'false_positive' ? 'Marked not drift' : 'Resolved',
            effect: null,
        };
    }
    const label = RESOLUTION_KIND_LABELS[kind];
    if (kind === 'auto_cleared') return { label, effect: 'no effect on detection' };
    if (kind === 'cause_fixed') {
        // A finding either declared its window or was attached to one.
        const w = windows.find((x) => x.drift_event_id === event.id || (x.drift_event_ids ?? []).includes(event.id));
        return {
            label,
            effect: w
                ? `incident window ${windowRangeText(w)} — its runs are not judged`
                : 'its incident window has since been withdrawn — those runs count again',
        };
    }
    const epoch = epochs.find((e) => e.drift_event_id === event.id);
    const since = epoch?.starts_at ?? (kind === 'accepted_change' ? event.change_point_at ?? event.detected_at : event.detected_at);
    return {
        label,
        effect:
            kind === 'accepted_change'
                ? `baseline re-learning since ${formatWhen(since)}`
                : `evidence up to ${formatWhen(since)} is not read again for this metric`,
    };
}

// ---------------------------------------------------------------------------
// The incident_window cause
// ---------------------------------------------------------------------------

export interface IncidentWindowCause {
    id: string | null;
    tenantWide: boolean;
    startsAt: string | null;
    endsAt: string | null;
    category: string | null;
    reason: string | null;
}

/** The window an `incident_window` attribution names (core `cause.rs`
 *  rung 0 evidence), or null when the evidence carries none. */
export function incidentWindowCause(evidence: unknown): IncidentWindowCause | null {
    if (!isRecord(evidence)) return null;
    const id = getString(evidence, 'incident_window_id') ?? null;
    const startsAt = getString(evidence, 'starts_at') ?? null;
    if (!id && !startsAt) return null;
    return {
        id,
        tenantWide: getBoolean(evidence, 'tenant_wide') ?? false,
        startsAt,
        endsAt: getString(evidence, 'ends_at') ?? null,
        category: getString(evidence, 'cause_category') ?? null,
        reason: getString(evidence, 'reason') ?? null,
    };
}

export function incidentCauseLabel(category?: string | null): string {
    if (!category) return 'Unspecified';
    return (INCIDENT_CAUSE_CATEGORIES as readonly string[]).includes(category)
        ? INCIDENT_CAUSE_LABELS[category as IncidentCauseCategory]
        : category;
}

/** "Provider outage, tenant-wide · <start> → <end> · <reason>" */
export function incidentWindowCauseText(c: IncidentWindowCause): string {
    const scope = c.tenantWide ? ', tenant-wide' : '';
    const range = windowRangeText({ starts_at: c.startsAt, ends_at: c.endsAt });
    return `${incidentCauseLabel(c.category)}${scope} · ${range}${c.reason ? ` · ${c.reason}` : ''}`;
}

// ---------------------------------------------------------------------------
// Windows as rendered facts
// ---------------------------------------------------------------------------

export function windowScopeText(w: Pick<IncidentWindow, 'tenant_wide' | 'ai_system_id' | 'ai_system_name'>): string {
    if (w.tenant_wide || !w.ai_system_id) return 'Every AI System in the tenant';
    return w.ai_system_name || w.ai_system_id;
}

/** Who declared it, when, and who last changed it. */
export function windowDeclaredText(
    w: Pick<IncidentWindow, 'declared_by' | 'declared_at' | 'updated_by' | 'updated_at' | 'drift_event_id'>,
): string {
    const how = w.drift_event_id ? 'declared by a cause-fixed resolution' : 'declared';
    const by = w.declared_by ? ` by ${w.declared_by}` : '';
    const at = w.declared_at ? ` on ${formatWhen(w.declared_at)}` : '';
    const edited = w.updated_at ? ` · edited${w.updated_by ? ` by ${w.updated_by}` : ''} ${formatWhen(w.updated_at)}` : '';
    return `${how}${by}${at}${edited}`;
}

/** A sealed record id, or the honest absence of one. */
export function sealText(recordId?: string | null): string {
    return recordId ? `sealed record ${recordId}` : 'not sealed';
}

// ---------------------------------------------------------------------------
// Charts
// ---------------------------------------------------------------------------

export interface ChartShade {
    id: string;
    /** `bucket_start` of the first and last bucket the window overlaps. */
    x1: string;
    x2: string;
}

/** Which chart buckets each incident window covers. A bucket is shaded
 *  when [bucket_start, bucket_start + step) overlaps [starts_at, ends_at);
 *  an open window runs to the end of the chart. Windows wholly outside the
 *  chart are dropped. */
export function incidentShading(
    windows: Pick<IncidentWindow, 'id' | 'starts_at' | 'ends_at'>[],
    points: Pick<RunTimeseriesPoint, 'bucket_start'>[],
    bucket: 'hour' | 'day',
): ChartShade[] {
    if (!points.length) return [];
    const step = bucket === 'hour' ? 3_600_000 : 86_400_000;
    const out: ChartShade[] = [];
    for (const w of windows) {
        if (!w.starts_at) continue;
        const s = new Date(w.starts_at).getTime();
        const e = w.ends_at ? new Date(w.ends_at).getTime() : Number.POSITIVE_INFINITY;
        if (Number.isNaN(s) || Number.isNaN(e) || e <= s) continue;
        const hit = points.filter((p) => {
            const b = new Date(p.bucket_start).getTime();
            return b < e && b + step > s;
        });
        if (!hit.length) continue;
        out.push({ id: w.id, x1: hit[0].bucket_start, x2: hit[hit.length - 1].bucket_start });
    }
    return out;
}

// ---------------------------------------------------------------------------
// The assurance report section (spec 1.8, additive)
// ---------------------------------------------------------------------------

export interface JudgementExclusions {
    incident_windows: IncidentWindow[];
    baseline_epochs: BaselineEpoch[];
    excluded_hours: number;
    excluded_runs: number;
    /** Approval holds raised inside the windows, not judged by the
     *  oversight rate. Null where the section does not state it. */
    excluded_approval_requests: number | null;
}

export interface ExcludedPeriod {
    incident_windows: number;
    excluded_hours: number;
    excluded_runs: number;
    statement: string;
}

/** `judgement_exclusions` from a report payload; null where the payload
 *  has no such section (the report schema makes it optional) rather than
 *  a guessed empty one. */
export function readJudgementExclusions(report: unknown): JudgementExclusions | null {
    const section = getRecord(report, 'judgement_exclusions');
    if (!section) return null;
    const windows = getArray(section, 'incident_windows');
    const epochs = getArray(section, 'baseline_epochs');
    if (!windows || !epochs) return null;
    return {
        // The report schema (spec 1.8) is the contract; the rows are
        // trusted once the discriminating keys are present.
        incident_windows: windows.filter(isRecord).map((w) => ({
            ...(w as unknown as IncidentWindow),
            runs_excluded: getNumber(w, 'runs_excluded') ?? 0,
            tenant_wide: getBoolean(w, 'tenant_wide') ?? !getString(w, 'ai_system_id'),
        })),
        baseline_epochs: epochs.filter(isRecord).map((e) => e as unknown as BaselineEpoch),
        excluded_hours: getNumber(section, 'excluded_hours') ?? 0,
        excluded_runs: getNumber(section, 'excluded_runs') ?? 0,
        excluded_approval_requests: getNumber(section, 'excluded_approval_requests') ?? null,
    };
}

/** The oversight line of "Excluded from judgement" (spec
 *  1.10): holds raised inside a window are recorded, not judged — and a
 *  window only excuses what it covers. Null when nothing was excluded. */
export function excludedHoldsText(n: number | null | undefined): string | null {
    if (!n) return null;
    return (
        `${n.toLocaleString()} approval request(s) raised inside the windows: recorded, not judged by the ` +
        'oversight rate. Holds raised outside every window count, however they ended.'
    );
}

/** `coverage.excluded_period` — the statement of what was NOT judged. */
export function readExcludedPeriod(report: unknown): ExcludedPeriod | null {
    const coverage = getRecord(report, 'coverage');
    const period = coverage ? getRecord(coverage, 'excluded_period') : undefined;
    const statement = period ? getString(period, 'statement') : undefined;
    if (!period || !statement) return null;
    return {
        incident_windows: getNumber(period, 'incident_windows') ?? 0,
        excluded_hours: getNumber(period, 'excluded_hours') ?? 0,
        excluded_runs: getNumber(period, 'excluded_runs') ?? 0,
        statement,
    };
}

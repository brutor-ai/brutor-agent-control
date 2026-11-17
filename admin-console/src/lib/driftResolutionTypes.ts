/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Drift resolution that the detector honours.
 *
 * Closing a drift finding takes a KIND, and each kind has exactly one
 * effect on detection:
 *
 *   accepted_change → a baseline epoch at the finding's change point: the
 *                     metric re-learns from there;
 *   cause_fixed     → an incident window [change point, fix]: its runs stay
 *                     recorded as evidence but are not judged;
 *   false_positive  → an epoch at detection time: the evidence the detector
 *                     misread is not read again for that metric.
 *
 * Wire shapes mirror `brutor-gateway-control`
 * `app/api/v1/models/admin/drift_resolution.py`; `api/compositionContract.ts`
 * pins every one against the generated OpenAPI schemas, so a field renamed
 * on either side fails `tsc`.
 */

import type { DependencySealStatus, DriftEvent } from './aiSystemTypes';

/** The kinds a human chooses when closing a finding. */
export type HumanResolutionKind = 'accepted_change' | 'cause_fixed' | 'false_positive';

/** `auto_cleared` is the control plane's own: a condition that cleared by
 *  itself. It has no effect on detection. */
export type ResolutionKind = HumanResolutionKind | 'auto_cleared';

export type IncidentCauseCategory = 'provider_outage' | 'platform_incident' | 'maintenance' | 'other';

/** Display-only narrowing of a window (the exclusion is by system / tenant). */
export type IncidentResourceType = 'model' | 'provider' | 'mcp_server' | 'skill' | 'knowledge_base' | 'agent';

/** The kinds that write a baseline epoch. `measurement_change` is
 *  written by the platform, never by a human: an upgrade changed how the
 *  metric is measured, so the baseline re-learns from the upgrade. */
export type EpochKind = 'accepted_change' | 'false_positive' | 'measurement_change'
    // A promoted contract changed what an outcome bucket means (its
    // evidence or whether it counts toward the baseline): that bucket
    // re-learns from the promotion.
    | 'catalogue_change';

/** `POST /v1/admin/drift/{id}/resolve` (and `/false-positive`). */
export interface CloseDriftRequest {
    kind: HumanResolutionKind;
    /** Required — the platform decides nothing silently. */
    note: string;
    /** cause_fixed only. Default: the change point, else detected_at. */
    window_starts_at?: string | null;
    /** cause_fixed only (exclusive). Default: now. */
    window_ends_at?: string | null;
    /** cause_fixed only. Default: other. */
    cause_category?: IncidentCauseCategory | null;
    /** cause_fixed only: the window covers every AI System in the tenant. */
    tenant_wide?: boolean;
    /** cause_fixed only, instead of the window fields: attach this close to
     *  an incident window already declared (a grouped card's other members). */
    incident_window_id?: string | null;
}

/** `ai_system_baseline_epochs` — samples before `starts_at` are excluded
 *  from the (metric, basis, caller) baseline, which re-learns from it. */
export interface BaselineEpoch {
    id: string;
    ai_system_id: string;
    metric_key: string;
    basis: string;
    /** `all` covers every caller of the metric. */
    caller_system_id: string;
    /** `all` covers every bucket of the metric; otherwise one outcome
     *  bucket (a full outcome id) — a `catalogue_change` epoch. */
    bucket: string;
    starts_at?: string | null;
    kind: EpochKind;
    drift_event_id?: string | null;
    /** measurement_change only: the platform registry entry. */
    measurement_change_id?: string | null;
    reason?: string | null;
    declared_by?: string | null;
    declared_at?: string | null;
    record_id?: string | null;
    sealed_at?: string | null;
}

export interface BaselineEpochListResponse {
    epochs: BaselineEpoch[];
    total: number;
}

/** `ai_system_incident_windows` — a declared period whose samples are
 *  excluded from judgement but kept as evidence. */
export interface IncidentWindow {
    id: string;
    /** Null = every AI System in the tenant. */
    ai_system_id?: string | null;
    ai_system_name?: string | null;
    tenant_wide: boolean;
    resource_type?: IncidentResourceType | null;
    resource_id?: string | null;
    starts_at?: string | null;
    /** Exclusive. */
    ends_at?: string | null;
    cause_category: IncidentCauseCategory;
    reason: string;
    /** Set when a `cause_fixed` resolution declared it. */
    drift_event_id?: string | null;
    /** Every cause_fixed finding this window closed: the declaring one
     *  first, then those attached to it (a grouped card's other members). */
    drift_event_ids?: string[];
    declared_by?: string | null;
    declared_at?: string | null;
    updated_by?: string | null;
    updated_at?: string | null;
    record_id?: string | null;
    sealed_at?: string | null;
    /** Runs whose start lies in the window — excluded from judgement, kept. */
    runs_excluded: number;
}

export interface IncidentWindowListResponse {
    windows: IncidentWindow[];
    total: number;
}

export interface IncidentWindowCreate {
    /** Tenant route only; omit (null) for every system in the tenant. */
    ai_system_id?: string | null;
    starts_at: string;
    ends_at: string;
    cause_category: IncidentCauseCategory;
    reason: string;
    resource_type?: IncidentResourceType | null;
    resource_id?: string | null;
}

/** Every edit is a new sealed declaration; the scope is not editable. */
export interface IncidentWindowUpdate {
    starts_at?: string | null;
    ends_at?: string | null;
    cause_category?: IncidentCauseCategory | null;
    reason?: string | null;
    resource_type?: IncidentResourceType | null;
    resource_id?: string | null;
}

export interface IncidentWindowWriteResponse extends IncidentWindow {
    seal: DependencySealStatus;
}

export interface IncidentWindowDeleteResponse {
    id: string;
    deleted: boolean;
    seal: DependencySealStatus;
}

/** The closed finding and the one effect its kind had on detection. */
export interface DriftResolution extends DriftEvent {
    baseline_epoch?: BaselineEpoch | null;
    incident_window?: IncidentWindow | null;
    seal: DependencySealStatus;
}

// ---------------------------------------------------------------------------
// Words
// ---------------------------------------------------------------------------

export const HUMAN_RESOLUTION_KINDS: readonly HumanResolutionKind[] = [
    'accepted_change',
    'cause_fixed',
    'false_positive',
];

export const RESOLUTION_KIND_LABELS: Record<ResolutionKind, string> = {
    accepted_change: 'Accepted change',
    cause_fixed: 'Cause fixed',
    false_positive: 'False positive',
    auto_cleared: 'Cleared on its own',
};

/** What each kind means, in the operator's words. */
export const RESOLUTION_KIND_MEANING: Record<HumanResolutionKind, string> = {
    accepted_change: 'The shift is intended — a release, a retune, a new caller. This is the new normal.',
    cause_fixed: 'Something was wrong — an outage, a bad deploy — and it has been fixed.',
    false_positive: 'The detector was wrong; nothing actually changed.',
};

/** One sentence per kind: its effect on detection. Shown where the choice
 *  is made, because the choice IS the effect. */
export const RESOLUTION_KIND_EFFECTS: Record<ResolutionKind, string> = {
    accepted_change:
        "This metric's baseline re-learns from the change point, so the same shift cannot fire again.",
    cause_fixed:
        'The period from the change point to the fix becomes an incident window: its runs stay as evidence but are not judged, and new bad behaviour after the fix still fires.',
    false_positive:
        'The evidence up to this detection is not read again for this metric, and the verdict feeds detector tuning.',
    auto_cleared: 'The condition cleared on its own; this has no effect on detection.',
};

export const INCIDENT_CAUSE_CATEGORIES: readonly IncidentCauseCategory[] = [
    'provider_outage',
    'platform_incident',
    'maintenance',
    'other',
];

export const INCIDENT_CAUSE_LABELS: Record<IncidentCauseCategory, string> = {
    provider_outage: 'Provider outage',
    platform_incident: 'Platform incident',
    maintenance: 'Maintenance',
    other: 'Other',
};

export const INCIDENT_RESOURCE_TYPES: readonly IncidentResourceType[] = [
    'model',
    'provider',
    'mcp_server',
    'skill',
    'knowledge_base',
    'agent',
];

export const INCIDENT_RESOURCE_LABELS: Record<IncidentResourceType, string> = {
    model: 'Model',
    provider: 'Provider',
    mcp_server: 'MCP server',
    skill: 'Skill',
    knowledge_base: 'Knowledge base',
    agent: 'Agent',
};

/** The epoch caller that covers every caller of a metric. */
export const EPOCH_ALL_CALLERS = 'all';

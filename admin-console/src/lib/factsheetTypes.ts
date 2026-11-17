/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * AI Asset Factsheet types — mirror the control-plane FactsheetResponse
 * served by `/v1/admin/assets/{id}/factsheet`. The
 * factsheet is a per-asset governance drill-down: a point-in-time roll-up
 * of usage, cost, guardrail, compliance, residency and access data for a
 * single governed AI asset over a configurable window, plus immutable
 * captured snapshots for audit evidence.
 */

export interface FactsheetUsage {
    requests: number;
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    estimated_cost_usd: number;
    success_count: number;
    error_count: number;
    error_rate: number;
    avg_duration_ms: number;
    first_seen: string | null;
    last_seen: string | null;
    /** Observed (imported via Traffic Data Import) split — included in the
     *  totals above, broken out so the planes stay distinguishable. */
    observed_requests: number;
    observed_cost_usd: number;
}

export interface FactsheetGuardrails {
    guardrail_blocked: number;
    pii_detected: number;
    argument_policy_denied: number;
}

export interface FactsheetCompliance {
    frameworks: Record<string, number>;
}

export interface FactsheetResidency {
    regions_routed_to: string[];
    residency_class_counts: Record<string, number>;
}

export interface FactsheetAccessResourceGroup {
    id: string;
    name: string | null;
}

export interface FactsheetAccess {
    resource_groups: FactsheetAccessResourceGroup[];
}

export interface FactsheetMeta {
    generated_at: string;
    period_hours: number;
    window_start: string | null;
    window_end: string;
}

export interface Factsheet {
    asset: Record<string, unknown>;
    config: Record<string, unknown>;
    usage: FactsheetUsage;
    guardrails: FactsheetGuardrails;
    compliance: FactsheetCompliance;
    residency: FactsheetResidency;
    access: FactsheetAccess;
    meta: FactsheetMeta;
}

export interface FactsheetSnapshot {
    id: string;
    tenant_id: string;
    asset_id: string;
    period_hours: number;
    snapshot_json: Factsheet;
    trigger: string | null;
    created_at: string | null;
}

export interface FactsheetSnapshotListResponse {
    snapshots: FactsheetSnapshot[];
    total: number;
}

/** Period-selector options, in period_hours. 0 = all time. */
export const FACTSHEET_PERIODS: { label: string; hours: number }[] = [
    { label: 'Last 24 hours', hours: 24 },
    { label: 'Last 7 days', hours: 168 },
    { label: 'Last 30 days', hours: 720 },
    { label: 'Last 90 days', hours: 2160 },
    { label: 'All time', hours: 0 },
];

export const FACTSHEET_DEFAULT_PERIOD_HOURS = 720;

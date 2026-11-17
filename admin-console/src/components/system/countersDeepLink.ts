/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Deep-link contract — URL builder for the System → Counters
 * page.
 *
 * Two callers today:
 *
 *   1. ProxyLogsViewer row action — operator sees a 429 (or any row
 *      with an enforced_limit_type populated) and clicks "View
 *      counters for this subject". Lands on the Counters page scoped
 *      to the bucket the proxy rejected against.
 *
 *   2. Mission Control alert detail — rate-limit family alerts
 *      (usage_rate_limit, concurrency_saturation, cooldown_armed)
 *      surface a deep-link button that constructs the same URL
 *      shape.
 *
 * The URL contract is documented in
 * `trial_bundle/docs/operations/deep-links.md`. The shape is:
 *
 *     /admin/system/counters
 *         ?cnt-scope=<scope>      // tenant|group|user|api_key|model|mcp_server
 *         &cnt-subject=<id>
 *         &cnt-kind=<kind>        // optional CounterKind
 *         &cnt-group=<group_id>   // optional
 *         &cnt-drawer=open        // optional: auto-open the detail drawer
 *
 * Stable for one year. New params can be added without breaking
 * existing PagerDuty playbooks because each is optional.
 */

import type { CounterKind, CounterScope } from '../../lib/systemOpsTypes';

export interface CountersDeepLinkInput {
    /** The bucket scope. Drives which counter family the Counters
     *  page suppresses to avoid showing off-scope rows. */
    scope?: CounterScope;
    /** Resource id matching the row's subject_id — e.g. a model id
     *  for scope=model, an MCP server id for scope=mcp_server. */
    subject?: string | null;
    /** Optional rate-limit kind (`llm_request_rate`, `llm_itpm`,
     *  `mcp_tool_rate`, …). When set, the Counters page narrows to
     *  the matching row. */
    kind?: CounterKind;
    /** Optional resource group id — when the alert is scoped to a
     *  group rather than a tenant-wide bucket. */
    groupId?: string | null;
    /** When true, the Counters page auto-opens the detail drawer
     *  on the single matching row. The default behaviour for
     *  forensic landings — operator goes straight to the
     *  Reset / Release controls. */
    openDrawer?: boolean;
}

/**
 * Construct the Counters page URL from an arbitrary subject
 * descriptor. Returns the path + query string (no host) so the link
 * works under both Vite dev and the production reverse-proxied
 * deploy.
 *
 * Empty / null params are silently omitted — the Counters page
 * tolerates a sparse URL (`/admin/system/counters` alone is the
 * normal mount path).
 *
 * The function does NOT validate the scope token against the
 * CounterScope union — TypeScript catches that at the call site.
 * At runtime we trust the caller.
 */
export function buildCountersDeepLinkUrl(
    input: CountersDeepLinkInput,
): string {
    const qs = new URLSearchParams();
    if (input.scope) qs.set('cnt-scope', input.scope);
    if (input.subject) qs.set('cnt-subject', input.subject);
    if (input.kind) qs.set('cnt-kind', input.kind);
    if (input.groupId) qs.set('cnt-group', input.groupId);
    if (input.openDrawer) qs.set('cnt-drawer', 'open');
    const search = qs.toString();
    return search
        ? `/admin/system/counters?${search}`
        : '/admin/system/counters';
}

// ---------------------------------------------------------------------------
// Mapping helpers — convert UI / wire types to the URL contract
// ---------------------------------------------------------------------------

/**
 * Source-row shape from `UnifiedProxyLogEntry`. We accept a tiny
 * structural type (not the full UnifiedProxyLogEntry) so the
 * Mission Control alert side can call the same mapper with its own
 * alert_details JSON. Future callers don't have to thread the proxy
 * log type around just to construct a deep-link URL.
 */
export interface DeepLinkSourceRow {
    /** 'llm', 'mcp', 'a2a', 'skill', … — the proxy_type column. */
    proxy_type?: string | null;
    /** Server id (MCP server, A2A server). */
    server_id?: string | null;
    /** LLM-specific block. */
    llm?: {
        model_used?: string | null;
        model_requested?: string | null;
    } | null;
    /** Resource group id. */
    resource_group_id?: string | null;
    /** HTTP status. 429 (rate-limited) is the canonical "show the
     *  deep link" trigger. */
    response?: {
        status_code?: number | null;
    } | null;
    /** The enforced-limit-type column. When the proxy
     *  rejected with a specific limit type the row carries the
     *  token here; we forward it as `kind` to land on the matching
     *  bucket row. Null on non-rejection rows. */
    enforced_limit_type?: string | null;
}

/**
 * Should we show the "View counters" action on this row?
 *
 * Two triggers:
 *   * HTTP 429 — anything the proxy rate-limited
 *   * `enforced_limit_type` populated — the audit column
 *     carries the rejection reason even on non-429 codepaths (e.g.
 *     LLM concurrency-slot rejections that surface as 503)
 *
 * Non-rate-limit rows return false; the operator can still navigate
 * to Counters via the sidebar.
 */
export function shouldShowCountersLink(row: DeepLinkSourceRow): boolean {
    if (row.response?.status_code === 429) return true;
    if (row.enforced_limit_type) return true;
    return false;
}

/**
 * Map a proxy-log row to the inputs `buildCountersDeepLinkUrl`
 * needs. Returns null when the row doesn't carry enough info to
 * land on a specific counter (in which case the caller should not
 * render the link).
 */
export function deepLinkInputFromProxyLogRow(
    row: DeepLinkSourceRow,
): CountersDeepLinkInput | null {
    const subject = subjectFromRow(row);
    const scope = scopeFromProxyType(row.proxy_type);
    if (!subject || !scope) return null;

    return {
        scope,
        subject,
        kind: (row.enforced_limit_type as CounterKind) ?? undefined,
        groupId: row.resource_group_id ?? undefined,
        openDrawer: true,
    };
}

function subjectFromRow(row: DeepLinkSourceRow): string | null {
    if (row.proxy_type === 'llm') {
        return (
            row.llm?.model_used ||
            row.llm?.model_requested ||
            row.server_id ||
            null
        );
    }
    return row.server_id ?? null;
}

function scopeFromProxyType(
    proxy_type: string | null | undefined,
): CounterScope | null {
    if (proxy_type === 'llm') return 'model';
    if (proxy_type === 'mcp') return 'mcp_server';
    // a2a, skill — these have rate limits but the counters page's
    // CounterScope doesn't carry tokens for them yet. Return null
    // so the deep link doesn't render for these rows; operator
    // navigates manually until those scopes are added.
    return null;
}

// ---------------------------------------------------------------------------
// Mission Control alert mappers
// ---------------------------------------------------------------------------

/**
 * The structural slice of `SystemAlert` we read. Mirroring only what's
 * needed lets this module stay free of a `groupTypes` import in tests
 * and reduces churn if SystemAlert grows new fields.
 */
export interface SystemAlertLike {
    error_code?: string | null;
    /** What the alert is about: 'llm_model' and 'mcp_server' are the
     *  two that map to a counter scope. */
    resource_type?: string | null;
    resource_id?: string | null;
    tenant_id?: string | null;
    context?: Record<string, unknown> | null;
}

/**
 * Rate-limit family detectors for SystemAlert. The proxy uses these
 * tokens consistently in `error_code` and in `context.kind`. We treat
 * any non-null match as "this alert is forensic-worthy on Counters".
 *
 * Lower-cased + underscore-normalised to dodge the SCREAMING_SNAKE
 * vs lower_snake skew between Rust and Python.
 */
const RATE_LIMIT_ERROR_CODE_TOKENS = new Set<string>([
    'usage_rate_limit',
    'usage_rate_limit_warning',
    'rate_limited',
    'llm_rate_limited',
    'mcp_rate_limited',
    'concurrency_saturation',
    'cooldown_armed',
    'cooldown_active',
]);

function normalizeToken(value: string | null | undefined): string {
    if (!value) return '';
    return value.toLowerCase().replace(/-/g, '_');
}

/**
 * Should the Mission Control alert row show a "View counters" deep
 * link? True only when the alert is a rate-limit / cooldown / quota
 * trigger we can map to a counter bucket. Pure infra-health and
 * downstream-IDP alerts return false — there's no counter to land on.
 */
export function shouldShowCountersLinkForAlert(
    alert: SystemAlertLike,
): boolean {
    if (!alert.resource_id) return false;
    const code = normalizeToken(alert.error_code);
    if (code && RATE_LIMIT_ERROR_CODE_TOKENS.has(code)) return true;
    const ctxLimit = alert.context?.['enforced_limit_type'];
    if (typeof ctxLimit === 'string' && ctxLimit.length > 0) return true;
    return false;
}

function scopeFromSystemAlertResource(
    resource_type: string | null | undefined,
): CounterScope | null {
    const t = normalizeToken(resource_type);
    if (t === 'llm_model') return 'model';
    if (t === 'mcp_server') return 'mcp_server';
    return null;
}

/**
 * Build the Counters deep-link input from a Mission Control system
 * alert. Returns null when the alert can't be mapped — caller hides
 * the button. The optional `enforced_limit_type` field in `context`
 * flows through as `kind` so the page
 * lands on the matching counter row.
 */
export function deepLinkInputFromSystemAlert(
    alert: SystemAlertLike,
): CountersDeepLinkInput | null {
    const scope = scopeFromSystemAlertResource(alert.resource_type);
    if (!scope || !alert.resource_id) return null;

    // The enforced_limit_type tag where the alert carries one; otherwise
    // the error_code itself, which may already be a CounterKind token.
    const ctxLimit = alert.context?.['enforced_limit_type'];
    const candidateKind =
        (typeof ctxLimit === 'string' ? ctxLimit : null) ??
        alert.error_code ??
        null;

    const ctxGroupId = alert.context?.['resource_group_id'];

    return {
        scope,
        subject: alert.resource_id,
        kind: (candidateKind ?? undefined) as CounterKind | undefined,
        groupId:
            (typeof ctxGroupId === 'string' && ctxGroupId.length > 0
                ? ctxGroupId
                : undefined) ?? undefined,
        openDrawer: true,
    };
}

// ---------------------------------------------------------------------------
// Usage alert mapper — UsageAlertInfo (quota / utilization)
// ---------------------------------------------------------------------------

/**
 * Structural slice of UsageAlertInfo. The quota alert always carries a
 * group_id but never a per-model subject — utilization is rolled up at
 * the resource-group level today. We deep-link to a group-scoped
 * Counters view so the operator sees every bucket the group has
 * pressure on.
 */
export interface UsageAlertLike {
    resource_type?: 'llm' | 'mcp' | string | null;
    group_id?: string | null;
    metric?: string | null;
}

export function shouldShowCountersLinkForUsageAlert(
    alert: UsageAlertLike,
): boolean {
    return Boolean(alert.group_id);
}

export function deepLinkInputFromUsageAlert(
    alert: UsageAlertLike,
): CountersDeepLinkInput | null {
    if (!alert.group_id) return null;
    // Usage alerts are quota-level — they don't pin a single subject.
    // Land on the group-scoped view; the operator can narrow further
    // via the sidebar facets once they're there.
    return {
        scope: 'group',
        subject: alert.group_id,
        kind: (alert.metric ?? undefined) as CounterKind | undefined,
        groupId: alert.group_id,
        openDrawer: false,
    };
}

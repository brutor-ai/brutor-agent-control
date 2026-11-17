/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Bucketing helper for proxy_metrics.proxy_type → display surface
 * (LLM / MCP / Portal / Other). Used by Mission Control's
 * "Total Requests" KPI subtitle and the "Requests by Surface" pie
 * chart so both views agree on what counts as what.
 *
 * Lives outside MissionControl.tsx and MissionControlCharts.tsx so
 * the chart can import without cycling through the page component.
 */

export interface SurfaceBreakdown {
    llm: number;
    mcp: number;
    portal: number;
    other: number;
}

/**
 * Roll the raw per-`proxy_type` counts the Rust gateway writes
 * (`build_proxy_metric` stamps `proxy_type` from
 * `constants::proxy::*`) into the four display buckets.
 *
 *   LLM    = `llm` (chat / embeddings / multimodal media /
 *            anthropic-native) + `batch` + `skill`
 *            (skills wrap an LLM call)
 *   MCP    = `mcp`
 *   Portal = `portal` (portal session / refresh) + `oauth`
 *            (OAuth token exchange the proxy runs on behalf of an
 *            MCP server). Both are auth flows, neither is billable
 *            resource-group activity — which is exactly why they
 *            show up in Mission Control's tenant-wide total but
 *            not in any per-group view.
 *   Other  = anything we don't recognise. Catches new subtypes
 *            without silently dropping them from the chart.
 *
 * A2A is *not* included here. A2A routes write only to
 * `proxy_logs`, not `proxy_metrics`, so `requests_by_proxy_type`
 * doesn't cover them. The KPI/chart layer pulls A2A counts from
 * `a2a_overview.total_requests` (a separate aggregation) and
 * stitches them in at render time.
 */
export function bucketRequestsBySurface(
    byType: Record<string, number> | undefined | null,
): SurfaceBreakdown {
    const t = byType ?? {};
    const llm = (t.llm ?? 0) + (t.batch ?? 0) + (t.skill ?? 0);
    const mcp = t.mcp ?? 0;
    const portal = (t.portal ?? 0) + (t.oauth ?? 0);
    const known = new Set(['llm', 'batch', 'skill', 'mcp', 'portal', 'oauth']);
    let other = 0;
    for (const [k, v] of Object.entries(t)) {
        if (!known.has(k)) other += v;
    }
    return { llm, mcp, portal, other };
}

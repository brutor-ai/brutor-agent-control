/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The single place where the proxy-log viewer's filter state becomes a wire
 * body. `/search`, `/facets` and the saved-view snapshot all go through it,
 * which is what guarantees the facet counts and the table describe the same
 * scope. The export takes the same envelope as its body, so the file holds
 * the rows the table shows.
 *
 * Kept apart from ProxyLogsViewer.tsx (as timeRangeToBody is) so the
 * 16-filter mapping can be asserted directly instead of only through a 1900
 * line component. The component keeps a `useCallback` wrapper over it.
 */

import type { TimeRange } from './TimeRangePicker';
import { timeRangeToBody } from './timeRangeToBody';

/**
 * The values of `proxy_logs.proxy_type`: exactly what the gateway writes, in
 * its order (`proxy::ALL` in brutor-gateway-core `src/constants.rs`), which
 * is exactly what the control plane's filter accepts (`skill` is one of
 * them, and a virtual server's rows are `virtual-mcp`, not `virtual_mcp`).
 * The test beside this file holds
 * the list to the control plane's own type.
 *
 * The Proxy type facet offers the values that are in the stored rows, not
 * this list. This list is what a click or a saved view is passed through, so
 * that a stray value in the column (an empty string) does not make the
 * control plane refuse the whole search.
 */
export const PROXY_TYPE_VALUES = ['mcp', 'virtual-mcp', 'llm', 'skill', 'oauth', 'a2a', 'batch', 'portal'] as const;
export type ProxyType = typeof PROXY_TYPE_VALUES[number];
export const isProxyType = (v: unknown): v is ProxyType =>
    (PROXY_TYPE_VALUES as readonly unknown[]).includes(v);

/**
 * The two only an Enterprise gateway writes: the batch surface and the
 * Portal API are mounted by its extensions. A Community gateway writes the
 * other six, so a Community console's facet, which lists what is stored,
 * never offers these.
 */
export const ENTERPRISE_GATEWAY_PROXY_TYPES = ['batch', 'portal'] as const satisfies readonly ProxyType[];

export interface ProxyLogFilterState {
    timeRange: TimeRange;
    proxyTypesFilter: readonly ProxyType[];
    /** Sidebar facet values, the strings 'true' / 'false'. */
    successFilter: readonly string[];
    serverFilter: readonly string[];
    gatewayUserFilter: readonly string[];
    idpUserFilter: readonly string[];
    targetUrlFilter: readonly string[];
    capabilityFilter: readonly string[];
    correlationIdFilter: string;
    sessionIdFilter: string;
    minDuration: string;
    maxDuration: string;
    complianceTagFilter: readonly string[];
    regionFilter: readonly string[];
    httpStatusFilter: readonly string[];
    freeText: string;
}

export interface ProxyLogFilterEnvelope {
    hours_ago?: number;
    start_time?: string;
    end_time?: string;
    proxy_types?: readonly ProxyType[];
    success?: boolean;
    server_ids?: readonly string[];
    gateway_user_ids?: readonly string[];
    idp_user_usernames?: readonly string[];
    target_urls?: readonly string[];
    mcp_capability_names?: readonly string[];
    correlation_id?: string;
    session_id?: string;
    min_duration_ms?: number;
    max_duration_ms?: number;
    compliance_tags_any?: readonly string[];
    region_routed_to?: readonly string[];
    http_status_codes?: number[];
    q?: string;
}

export function buildProxyLogFilterEnvelope(
    s: ProxyLogFilterState,
): ProxyLogFilterEnvelope {
    return {
        ...timeRangeToBody(s.timeRange),
        proxy_types: s.proxyTypesFilter.length > 0 ? s.proxyTypesFilter : undefined,
        // Sidebar facet values come back as the strings 'true'/'false'.
        // One selected → bool; both or empty → no filter.
        success: s.successFilter.length === 1
            ? s.successFilter[0] === 'true'
            : undefined,
        server_ids: s.serverFilter.length > 0 ? s.serverFilter : undefined,
        gateway_user_ids: s.gatewayUserFilter.length > 0 ? s.gatewayUserFilter : undefined,
        idp_user_usernames: s.idpUserFilter.length > 0 ? s.idpUserFilter : undefined,
        target_urls: s.targetUrlFilter.length > 0 ? s.targetUrlFilter : undefined,
        mcp_capability_names: s.capabilityFilter.length > 0 ? s.capabilityFilter : undefined,
        correlation_id: s.correlationIdFilter || undefined,
        session_id: s.sessionIdFilter || undefined,
        min_duration_ms: s.minDuration ? parseFloat(s.minDuration) : undefined,
        max_duration_ms: s.maxDuration ? parseFloat(s.maxDuration) : undefined,
        // Plural: backend treats `compliance_tags_any` as
        // OR-overlap against the JSONB array column.
        compliance_tags_any: s.complianceTagFilter.length > 0 ? s.complianceTagFilter : undefined,
        // Region is a string column; HTTP status is an int column, so
        // coerce the facet's string values to numbers for the backend's
        // `http_status_codes: List[int]`.
        region_routed_to: s.regionFilter.length > 0 ? s.regionFilter : undefined,
        http_status_codes: s.httpStatusFilter.length > 0
            ? s.httpStatusFilter.map(Number).filter((n) => Number.isFinite(n))
            : undefined,
        q: s.freeText || undefined,
    };
}

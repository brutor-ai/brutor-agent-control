/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * ProxyLogsViewer
 *
 * - Reads gateway_user_id, idp_user_id and idp_user_username
 * - Compact table layout with small fonts
 * - Columns: Time, Principal (gateway user / group-scoped key on top,
 *   IdP identity beneath), Resource, Method (HTTP verb only), Target (upstream URL),
 *   Compliance & Residency (tags + region/residency-class), Capability
 *   (tool/resource name, the MCP JSON-RPC method for connect/discovery
 *   handshakes, or the response-cache op on an LLM call), Duration,
 *   HTTP Status, Size
 * - Expanded row shows: Target URL, Client IP, Request Preview, Response Preview, Duration, Session ID
 */

import { GatewayRefusalNote } from './GatewayRefusalNote';
import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import {
    Download,
    RefreshCw,
    Filter,
    CheckCircle,
    XCircle,
    AlertTriangle,
    ChevronDown,
    ChevronRight,
    Loader,
    ArrowUpDown,
    ArrowDown,
    ArrowUp,
    Eye,
    GitBranch,
    Search,
    X
} from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import ProxyLogDetailsModal from './ProxyLogDetailsModal';
import { TABLE_TH_TYPE } from '../ui/tableStyles';
import TraceTimelineModal from './TraceTimelineModal';
// Deep-link from a rate-limited proxy log row to the
// matching counter bucket on System → Counters. Pure helpers; the
// link only renders when shouldShowCountersLink returns true.
import {
    deepLinkInputFromProxyLogRow,
    shouldShowCountersLink,
} from '../system/countersDeepLink';
import { CountersDeepLinkButton } from '../system/CountersDeepLinkButton';
// ActiveFilterChips renders the chip row above the table; the value
// pickers are FacetSidebar's.
import { ActiveFilterChips } from './FacetMultiSelect';
import TimeRangePicker, { TimeRange } from './TimeRangePicker';
import {
    buildProxyLogFilterEnvelope,
    isProxyType,
    type ProxyType,
} from './proxyLogFilterEnvelope';
import FacetSidebar from './FacetSidebar';
import SavedViewMenu from './SavedViewMenu';
import { useAuth } from '../../hooks/useAuth';
import {
    readFiltersFromUrl,
    useUrlFilterSync,
    encodeStringArray, decodeStringArray,
    encodeString, decodeString,
    encodeTimeRange, decodeTimeRange,
} from './useUrlFilterState';
import {
    UnifiedProxyLogEntry,
    ProxyLogsQueryParams,
    formatDuration,
    formatBytes,
    getCapabilityDisplay
} from '../../lib/types';
import { errorKind, errorMessage } from '../../lib/narrow';
import { refusedCredentialMark, refusedCredentialOf, refusedCredentialSentence } from '../../lib/refusedCredential';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';


/** An empty body has four different reasons, and "No preview
 *  available" hid all of them. Say which. */
function bodyStateText(state?: string): string {
    switch (state) {
        case 'withheld':
            return 'Withheld — your role can read log rows but not their bodies (proxy:logs:read_bodies).';
        case 'pruned':
            return 'Removed by the tenant retention policy. The record and its hash remain.';
        case 'erased':
            return 'Erased on a data-subject request. The record and its hash remain.';
        case 'none':
            return 'Not stored — the tenant stores no bodies, or the call carried none.';
        default:
            return 'No preview available';
    }
}

interface ProxyLogsViewerProps {
    onRefresh?: () => void;
    onDownload?: () => void;
}

const ProxyLogsViewer: React.FC<ProxyLogsViewerProps> = ({
                                                             onRefresh,
                                                             onDownload
                                                         }) => {
    // ============================================================================
    // STATE MANAGEMENT
    // ============================================================================

    const [logs, setLogs] = useState<UnifiedProxyLogEntry[]>([]);
    const [pagination, setPagination] = useState({
        total: 0,
        offset: 0,
        limit: 50, // Reduced to 50 for better performance with small fonts
        returned: 0,
        has_more: false
    });
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // Filter states.
    //
    // Faceted filters are multi-value (Datadog-style) — each holds
    // the operator's selected values. The empty array `[]` means "no filter" (sent to the
    // backend as omitted; the operator's table view is unscoped on
    // that field).
    const [proxyTypesFilter, setProxyTypesFilter] = useState<ProxyType[]>([]);
    // Status is a sidebar facet driven by proxy_logs.success (bool).
    // Empty / both selected = no filter; ['true'] = success only;
    // ['false'] = failure only.
    const [successFilter, setSuccessFilter] = useState<string[]>([]);
    const [serverFilter, setServerFilter] = useState<string[]>([]);
    const [gatewayUserFilter, setGatewayUserFilter] = useState<string[]>([]);
    const [idpUserFilter, setIdpUserFilter] = useState<string[]>([]);
    const [targetUrlFilter, setTargetUrlFilter] = useState<string[]>([]);
    const [capabilityFilter, setCapabilityFilter] = useState<string[]>([]);
    const [correlationIdFilter, setCorrelationIdFilter] = useState<string>('');
    // The conversation scope, via the API's existing `session_id` filter
    // (which queries proxy_logs.end_user_session_id). correlation_id
    // identifies ONE request; this spans every run of one chat.
    //
    // Clients assert a real conversation id, so the filter needs a way to
    // be USED, which is the chip + click-through below.
    const [sessionIdFilter, setSessionIdFilter] = useState<string>('');
    // Which session's budget reset is in flight, and the last outcome. A
    // reset moves money, so it confirms first and reports after — a silent
    // success here is indistinguishable from a silent no-op.
    const [resettingSession, setResettingSession] = useState<string | null>(null);
    const [resetNotice, setResetNotice] = useState<string>('');
    // Free-text substring search across body previews + target_url +
    // error_message. Hits a GIN tsvector index server-side.
    const [freeTextSearch, setFreeTextSearch] = useState<string>('');
    // Only the settled value reaches the filter envelope. The raw state drives
    // the input so typing stays responsive; without the split, every keystroke
    // is a fresh tsvector scan and a fresh chance for a stale response to win.
    const debouncedFreeTextSearch = useDebouncedValue(freeTextSearch, 300);
    const [minDuration, setMinDuration] = useState<string>('');
    const [maxDuration, setMaxDuration] = useState<string>('');
    // Time range: preset (Last 24h, etc.) or operator-supplied custom
    // window. The TimeRangePicker tracks both modes; the envelope
    // builder uses `timeRangeToBody` to emit the right wire fields.
    const [timeRange, setTimeRange] = useState<TimeRange>({ kind: 'preset', minutes: 60 * 24 });
    // Compliance tags are a sidebar facet. Multiple selectable;
    // empty = no filter. Backend `compliance_tags_any` is the OR-overlap
    // operator against the JSONB array column, so multiple values
    // mean "any of these tags".
    const [complianceTagFilter, setComplianceTagFilter] = useState<string[]>([]);
    // Region (region_routed_to) + HTTP status code sidebar facets. These
    // are real, filterable scalar columns — the backend coalesces
    // `region_routed_to` / `http_status_codes` into IN() clauses, and this
    // state is what a click on one of them sets.
    const [regionFilter, setRegionFilter] = useState<string[]>([]);
    const [httpStatusFilter, setHttpStatusFilter] = useState<string[]>([]);

    // ── Resizable layout: facet rail width + per-column widths ──────────
    // Drag the splitter right of the facet rail to give the log table
    // more room; drag a column header's right edge to resize that column.
    // Both persist to localStorage so the operator's layout survives a
    // refresh. The 11 column widths map 1:1 to the header columns:
    // expand · time · user · resource · method · target · compliance ·
    // capability · duration · status · size.
    const [facetWidth, setFacetWidth] = useState<number>(() => {
        const s = Number(localStorage.getItem('brutor.proxylogs.facetwidth'));
        return s >= 160 && s <= 600 ? s : 240;
    });
    const [colWidths, setColWidths] = useState<number[]>(() => {
        const defaults = [32, 132, 150, 120, 70, 240, 180, 150, 80, 90, 110];
        try {
            const parsed = JSON.parse(localStorage.getItem('brutor.proxylogs.colwidths') || 'null');
            if (Array.isArray(parsed) && parsed.length === defaults.length &&
                parsed.every((n) => typeof n === 'number' && n > 0)) {
                return parsed as number[];
            }
        } catch { /* fall through to defaults */ }
        return defaults;
    });
    useEffect(() => {
        localStorage.setItem('brutor.proxylogs.facetwidth', String(facetWidth));
    }, [facetWidth]);
    useEffect(() => {
        localStorage.setItem('brutor.proxylogs.colwidths', JSON.stringify(colWidths));
    }, [colWidths]);

    // `kind === 'facet'` resizes the rail; a number resizes that column.
    const dragRef = useRef<{ startX: number; startVal: number; kind: 'facet' | number } | null>(null);
    const onResizeMove = useCallback((e: MouseEvent) => {
        const d = dragRef.current;
        if (!d) return;
        const dx = e.clientX - d.startX;
        if (d.kind === 'facet') {
            setFacetWidth(Math.min(600, Math.max(160, d.startVal + dx)));
        } else {
            const idx = d.kind;
            setColWidths((prev) => {
                const next = prev.slice();
                next[idx] = Math.min(900, Math.max(40, d.startVal + dx));
                return next;
            });
        }
    }, []);
    const onResizeEnd = useCallback(() => {
        dragRef.current = null;
        document.removeEventListener('mousemove', onResizeMove);
        document.removeEventListener('mouseup', onResizeEnd);
        document.body.style.userSelect = '';
        document.body.style.cursor = '';
    }, [onResizeMove]);
    const startResize = useCallback((kind: 'facet' | number) => (e: React.MouseEvent) => {
        e.preventDefault();
        e.stopPropagation();
        const startVal = kind === 'facet' ? facetWidth : colWidths[kind];
        dragRef.current = { startX: e.clientX, startVal, kind };
        document.addEventListener('mousemove', onResizeMove);
        document.addEventListener('mouseup', onResizeEnd);
        document.body.style.userSelect = 'none';
        document.body.style.cursor = 'col-resize';
    }, [facetWidth, colWidths, onResizeMove, onResizeEnd]);

    // UI states
    const [expandedRow, setExpandedRow] = useState<string | null>(null);
    // The row whose full-data modal is open. Distinct from
    // expandedRow so opening Details doesn't accidentally collapse
    // the inline expansion underneath.
    const [detailsLog, setDetailsLog] = useState<UnifiedProxyLogEntry | null>(null);
    // Open the trace timeline view for a row's
    // root_task_id. Hidden when the row has no chain stamped.
    const [traceRootId, setTraceRootId] = useState<string | null>(null);
    const [sortField, setSortField] = useState<string>('timestamp');
    const [sortDirection, setSortDirection] = useState<'asc' | 'desc'>('desc');
    const [showFilters, setShowFilters] = useState(true);

    // Use the useGateway hook to get proxy logs methods
    const { fetchProxyLogs, exportProxyLogs, gatewayClient } = useGateway();

    /**
     * Clear a conversation's spend against its session budget.
     *
     * Confirms first: this changes what the gateway will let the user spend.
     * The confirm text says what is NOT happening as well as what is —
     * "reset" reads like "delete" to most people, and the ledger rows stay.
     */
    const handleResetSessionBudget = useCallback(
        async (sessionId: string) => {
            const ok = window.confirm(
                `Reset the spend budget for session ${sessionId.substring(0, 16)}…?\n\n` +
                    'The conversation can spend again from zero.\n' +
                    'Its request history is NOT deleted — the logs stay exactly as they are.',
            );
            if (!ok) return;
            setResettingSession(sessionId);
            setResetNotice('');
            try {
                const res = await gatewayClient.resetSessionBudget(
                    sessionId,
                    'Reset from Proxy Logs',
                );
                setResetNotice(
                    `Budget reset for ${sessionId.substring(0, 16)}… at ` +
                        `${new Date(res.reset_at).toLocaleString()}. History retained.`,
                );
            } catch (e) {
                setResetNotice(
                    `Reset failed: ${e instanceof Error ? e.message : String(e)}`,
                );
            } finally {
                setResettingSession(null);
            }
        },
        [gatewayClient],
    );

    /**
     * Build the request body shape both /search and /facets expect.
     * Keeping this in one place means a future filter addition only
     * needs to be wired in once — and it guarantees the facet
     * counts always reflect the same scope as the table.
     */
    const buildFilterEnvelope = useCallback(() => buildProxyLogFilterEnvelope({
        timeRange,
        proxyTypesFilter,
        successFilter,
        serverFilter,
        gatewayUserFilter,
        idpUserFilter,
        targetUrlFilter,
        capabilityFilter,
        correlationIdFilter,
        sessionIdFilter,
        minDuration,
        maxDuration,
        complianceTagFilter,
        regionFilter,
        httpStatusFilter,
        freeText: debouncedFreeTextSearch,
    }), [
        timeRange, proxyTypesFilter, successFilter, serverFilter,
        gatewayUserFilter, idpUserFilter, targetUrlFilter, capabilityFilter,
        correlationIdFilter, sessionIdFilter, minDuration, maxDuration, complianceTagFilter,
        regionFilter, httpStatusFilter,
        debouncedFreeTextSearch,
    ]);

    /**
     * Wrappers passed down to each FacetMultiSelect so the dropdowns
     * fetch top-N values + typeahead matches from the same /facets
     * and /suggest endpoints, scoped to the current filter envelope.
     * Without scoping, the dropdown for "Capability" would list
     * every capability the tenant has ever used — even those filtered
     * out by the active time range — which is the audit-log search
     * antipattern Datadog calls "the haunted dropdown".
     */
    // ID→Name labels resolved server-side per /facets call. Keeps the
    // filter values as IDs (what /search accepts) and renders the
    // looked-up name in the UI. Merged across fields because the
    // FacetSidebar's `labels` prop is a flat map (ID spaces don't
    // collide — each source table uses its own prefixed ULIDs).
    //
    // Pre-seeded with the status facet values: proxy_logs.success
    // comes back from /facets as the strings 'true' / 'false' (the
    // sidebar's valueToKey() coerces booleans). The labels here
    // turn them into the operator-facing 'Success' / 'Failure'.
    const [displayLabels, setDisplayLabels] = useState<Record<string, string>>({
        'true': 'Success',
        'false': 'Failure',
    });

    const loadFacets = useCallback(async (field: string) => {
        if (!gatewayClient) return [];
        const body = { ...buildFilterEnvelope(), fields: [field], per_field_limit: 50 };
        const res = await gatewayClient.fetchProxyLogFacets(body);
        // Stash any newly-resolved display names into the flat label
        // map. setState is a functional update so concurrent loads
        // from multiple expanded sections compose correctly without
        // clobbering each other.
        let perField = res.display_names?.[field];
        // Gateway user: show "name (id)" rather than just the resolved
        // name, so the operator keeps the raw id for cross-referencing.
        // The value resolves to a username (user/JWT keys) or a resource
        // group name (group-scoped API keys) — the overloaded
        // gateway_user_id column carries either — so it matches the
        // proxy-log "Gateway/Idp User" column either way.
        if (field === 'gateway_user_id' && perField) {
            perField = Object.fromEntries(
                Object.entries(perField).map(([id, name]) => [id, `${name} (${id})`]),
            );
        }
        if (perField && Object.keys(perField).length > 0) {
            setDisplayLabels(prev => ({ ...prev, ...perField }));
        }
        return (res.facets?.[field] ?? []).map(r => ({ value: r.value, count: r.count }));
    }, [gatewayClient, buildFilterEnvelope]);

    // ------------------------------------------------------------------
    // Sidebar wiring. The sidebar shares loadFacets with the inline
    // FacetMultiSelect dropdowns (single source of truth for values
    // + counts). Each sidebar section delegates clicks to the matching
    // state setter via `sidebarSelected` / `sidebarOnChange` below.
    //
    // `refetchKey` is a cheap hash of the filter envelope so the
    // sidebar's open sections re-fetch whenever the operator narrows
    // — without us subscribing to every individual filter state.
    //
    // The standalone "LLM model" facet has been folded into "Resource"
    // — proxy_logs.server_id already carries the LLM model id on
    // proxy_type=llm rows, so listing both would double-count and
    // confuse operators.
    // ------------------------------------------------------------------
    const sidebarFields = useMemo(() => ([
        { field: 'server_id',           label: 'Resource' },
        { field: 'gateway_user_id',     label: 'Gateway user' },
        { field: 'idp_user_username',   label: 'IdP user' },
        { field: 'target_url',          label: 'Target URL' },
        { field: 'mcp_capability_name', label: 'Capability' },
        { field: 'proxy_type',          label: 'Proxy type' },
        { field: 'success',             label: 'Status' },
        { field: 'compliance_tags',     label: 'Compliance' },
        { field: 'http_status_code',    label: 'HTTP status' },
        { field: 'region_routed_to',    label: 'Region' },
    ]), []);

    const sidebarSelected = useMemo(() => ({
        server_id:           serverFilter,
        gateway_user_id:     gatewayUserFilter,
        idp_user_username:   idpUserFilter,
        target_url:          targetUrlFilter,
        mcp_capability_name: capabilityFilter,
        proxy_type:          proxyTypesFilter,
        success:             successFilter,
        compliance_tags:     complianceTagFilter,
        region_routed_to:    regionFilter,
        http_status_code:    httpStatusFilter,
    }), [
        serverFilter, gatewayUserFilter, idpUserFilter,
        targetUrlFilter, capabilityFilter, proxyTypesFilter,
        successFilter, complianceTagFilter, regionFilter, httpStatusFilter,
    ]);

    const handleSidebarChange = useCallback((field: string, next: string[]) => {
        switch (field) {
            case 'server_id':           setServerFilter(next); break;
            case 'gateway_user_id':     setGatewayUserFilter(next); break;
            case 'idp_user_username':   setIdpUserFilter(next); break;
            case 'target_url':          setTargetUrlFilter(next); break;
            case 'mcp_capability_name': setCapabilityFilter(next); break;
            case 'proxy_type':
                // Filter to known-valid enum values. /facets can
                // surface an empty string or a value outside the enum,
                // which the backend's LogQueryRequest would reject.
                setProxyTypesFilter(next.filter(isProxyType));
                break;
            case 'success':             setSuccessFilter(next); break;
            case 'compliance_tags':     setComplianceTagFilter(next); break;
            case 'region_routed_to':    setRegionFilter(next); break;
            case 'http_status_code':    setHttpStatusFilter(next); break;
            default: break;
        }
    }, []);

    // Cheap stable hash so the sidebar re-fetches when ANY active
    // filter or time range changes. Using JSON.stringify avoids
    // accidental shallow-equality misses across the dependency
    // matrix and keeps the sidebar counts honest.
    const sidebarRefetchKey = useMemo(
        () => JSON.stringify(buildFilterEnvelope()),
        [buildFilterEnvelope],
    );

    // ============================================================================
    // DATA FETCHING
    // ============================================================================

    // Monotonic request sequence. Responses are not guaranteed to arrive in
    // the order they were issued: a broad filter's /search can take seconds
    // while the narrower one the operator just picked returns immediately, and
    // the 30s auto-refresh keeps a second request in flight regardless. Without
    // this guard the slow response lands last and repaints the table with rows
    // for a filter the chips no longer show. Only the newest request may write.
    const fetchSeqRef = useRef(0);

    const fetchLogs = useCallback(async (newOffset: number = 0) => {
        if (!fetchProxyLogs) return;

        const seq = ++fetchSeqRef.current;

        setLoading(true);
        setError(null);

        try {
            // The multi-value filter envelope, with the page and the sort.
            const params: ProxyLogsQueryParams = {
                ...buildFilterEnvelope(),
                limit: pagination.limit,
                offset: newOffset,
                sort_by: sortField,
                sort_order: sortDirection,
            };

            const response = await fetchProxyLogs(params);

            if (seq !== fetchSeqRef.current) return;

            setLogs(response.logs);
            setPagination(response.pagination);
        } catch (err) {
            if (seq !== fetchSeqRef.current) return;
            console.error('Failed to fetch logs:', errorKind(err));
            setError(errorMessage(err, 'Failed to fetch logs'));
        } finally {
            // A superseded request must not clear the spinner the newer one set.
            if (seq === fetchSeqRef.current) setLoading(false);
        }
    }, [
        fetchProxyLogs,
        buildFilterEnvelope,
        sortField,
        sortDirection,
        pagination.limit
    ]);

    // URL hydration — on mount, read filter state out of the URL
    // query string and apply to local state. Runs exactly once;
    // subsequent navigations are handled by useUrlFilterSync below.
    // This is what makes a Datadog-style shareable link work.
    useEffect(() => {
        const fromUrl = readFiltersFromUrl('p_', {
            servers:        decodeStringArray,
            gateway_users:  decodeStringArray,
            idp_users:      decodeStringArray,
            target_urls:    decodeStringArray,
            capabilities:   decodeStringArray,
            compliance:     decodeStringArray,
            success:        decodeStringArray,
            q:              decodeString,
            time:           decodeTimeRange(60 * 24),
        });
        if (fromUrl) {
            setServerFilter(fromUrl.servers);
            setGatewayUserFilter(fromUrl.gateway_users);
            setIdpUserFilter(fromUrl.idp_users);
            setTargetUrlFilter(fromUrl.target_urls);
            setCapabilityFilter(fromUrl.capabilities);
            setComplianceTagFilter(fromUrl.compliance);
            setSuccessFilter(fromUrl.success);
            setFreeTextSearch(fromUrl.q);
            setTimeRange(fromUrl.time);
        }
         
    }, []);

    // URL sync — push filter state back into ?p_… params whenever
    // it changes, via history.replaceState (no spam). Combined with
    // the hydrate-on-mount block above, this gives Datadog/Sentry-
    // style shareable URLs.
    useUrlFilterSync(
        'p_',
        {
            servers:       serverFilter,
            gateway_users: gatewayUserFilter,
            idp_users:     idpUserFilter,
            target_urls:   targetUrlFilter,
            capabilities:  capabilityFilter,
            compliance:    complianceTagFilter,
            success:       successFilter,
            q:             freeTextSearch,
            time:          timeRange,
        },
        {
            servers:       encodeStringArray,
            gateway_users: encodeStringArray,
            idp_users:     encodeStringArray,
            target_urls:   encodeStringArray,
            capabilities:  encodeStringArray,
            compliance:    encodeStringArray,
            success:       encodeStringArray,
            q:             encodeString,
            time:          encodeTimeRange(60 * 24),
        },
    );

    // Auto-fetch on mount and filter changes
    useEffect(() => {
        runBackground(fetchLogs(0), 'logs');
    }, [fetchLogs]);

    // ============================================================================
    // EVENT HANDLERS
    // ============================================================================

    const handleRefresh = () => {
        runBackground(fetchLogs(0), 'logs');
        onRefresh?.();
    };

    const handleExport = async (format: 'csv' | 'jsonl' = 'csv') => {
        if (!exportProxyLogs) return;

        // The export takes the body of the search: the file holds the rows
        // the table shows, in its order.
        try {
            const { blob, filename } = await exportProxyLogs(
                { ...buildFilterEnvelope(), sort_by: sortField, sort_order: sortDirection },
                format,
            );
            const fallback = `proxy-logs-${new Date().toISOString().replace(/[:.]/g, '-')}.${format}`;
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = filename || fallback;
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);

            onDownload?.();
        } catch (err) {
            console.error('Failed to export logs:', errorKind(err));
            setError(errorMessage(err, 'Failed to export logs'));
        }
    };

    const handleSort = (field: string) => {
        if (sortField === field) {
            setSortDirection(sortDirection === 'asc' ? 'desc' : 'asc');
        } else {
            setSortField(field);
            setSortDirection('desc');
        }
    };

    const toggleRowExpansion = (logId: string) => {
        setExpandedRow(expandedRow === logId ? null : logId);
    };

    const handleClearFilters = () => {
        setProxyTypesFilter([]);
        setSuccessFilter([]);
        setServerFilter([]);
        setGatewayUserFilter([]);
        setIdpUserFilter([]);
        setTargetUrlFilter([]);
        setCapabilityFilter([]);
        setCorrelationIdFilter('');
        setSessionIdFilter('');
        setMinDuration('');
        setMaxDuration('');
        setTimeRange({ kind: 'preset', minutes: 60 * 24 });
        setComplianceTagFilter([]);
        setRegionFilter([]);
        setHttpStatusFilter([]);
        setFreeTextSearch('');
    };

    /**
     * Apply a saved view's stored envelope to local filter state.
     * The envelope shape mirrors `buildFilterEnvelope()`'s output —
     * each known key goes back into its setter, and a key that is not
     * one of the envelope's is ignored.
     */
    const handleApplySavedView = useCallback((filters: Record<string, any>) => {
        // Same allowlist guard as the sidebar dispatcher — a stored
        // view may carry a value outside the enum, or '', which would
        // otherwise break the next /search.
        setProxyTypesFilter(
            Array.isArray(filters.proxy_types)
                ? filters.proxy_types.filter(isProxyType)
                : [],
        );
        // Status: scalar bool in the envelope round-trips to the
        // sidebar's ['true'] / ['false'] / [] shape.
        setSuccessFilter(
            filters.success === true ? ['true']
            : filters.success === false ? ['false']
            : [],
        );
        setServerFilter(Array.isArray(filters.server_ids) ? filters.server_ids : []);
        setGatewayUserFilter(Array.isArray(filters.gateway_user_ids) ? filters.gateway_user_ids : []);
        setIdpUserFilter(Array.isArray(filters.idp_user_usernames) ? filters.idp_user_usernames : []);
        setTargetUrlFilter(Array.isArray(filters.target_urls) ? filters.target_urls : []);
        setCapabilityFilter(Array.isArray(filters.mcp_capability_names) ? filters.mcp_capability_names : []);
        setCorrelationIdFilter(typeof filters.correlation_id === 'string' ? filters.correlation_id : '');
        setSessionIdFilter(typeof filters.session_id === 'string' ? filters.session_id : '');
        setMinDuration(filters.min_duration_ms != null ? String(filters.min_duration_ms) : '');
        setMaxDuration(filters.max_duration_ms != null ? String(filters.max_duration_ms) : '');
        setComplianceTagFilter(Array.isArray(filters.compliance_tags_any) ? filters.compliance_tags_any : []);
        setRegionFilter(Array.isArray(filters.region_routed_to) ? filters.region_routed_to : []);
        setHttpStatusFilter(
            Array.isArray(filters.http_status_codes)
                ? filters.http_status_codes.map(String)
                : [],
        );
        setFreeTextSearch(typeof filters.q === 'string' ? filters.q : '');
        // Time range — preset (hours_ago) or custom (start_time/end_time).
        if (filters.start_time && filters.end_time) {
            setTimeRange({ kind: 'custom', start: String(filters.start_time), end: String(filters.end_time) });
        } else if (typeof filters.hours_ago === 'number') {
            setTimeRange({ kind: 'preset', minutes: filters.hours_ago * 60 });
        } else {
            setTimeRange({ kind: 'preset', minutes: 60 * 24 });
        }
    }, []);

    const { user } = useAuth();
    const currentAdminId = user?.id ?? null;

    // Snapshot of the live envelope for SavedViewMenu's "save current"
    // action. Memoised on the envelope builder so the menu captures
    // exactly what /search would have used at click time.
    const currentFiltersForSave = useMemo(
        () => buildFilterEnvelope(),
        [buildFilterEnvelope],
    );

    /**
     * Remove a single (field, value) pair — used by the
     * ActiveFilterChips X button. The chip → state-setter mapping is
     * hard-coded because we have a small fixed set of facet fields;
     * a runtime lookup would just be a `switch` either way.
     */
    const handleRemoveFilter = (field: string, value: string) => {
        switch (field) {
            case 'server_id':
                setServerFilter((prev) => prev.filter((v) => v !== value));
                break;
            case 'gateway_user_id':
                setGatewayUserFilter((prev) => prev.filter((v) => v !== value));
                break;
            case 'idp_user_username':
                setIdpUserFilter((prev) => prev.filter((v) => v !== value));
                break;
            case 'target_url':
                setTargetUrlFilter((prev) => prev.filter((v) => v !== value));
                break;
            case 'mcp_capability_name':
                setCapabilityFilter((prev) => prev.filter((v) => v !== value));
                break;
            case 'proxy_type':
                setProxyTypesFilter((prev) => prev.filter((v) => v !== value));
                break;
            case 'compliance_tags':
                setComplianceTagFilter((prev) => prev.filter((v) => v !== value));
                break;
            case 'success':
                setSuccessFilter((prev) => prev.filter((v) => v !== value));
                break;
            default:
                break;
        }
    };

    const handleNextPage = () => {
        if (pagination.has_more) {
            const newOffset = pagination.offset + pagination.limit;
            runBackground(fetchLogs(newOffset), 'logs');
        }
    };

    const handlePrevPage = () => {
        if (pagination.offset > 0) {
            const newOffset = Math.max(0, pagination.offset - pagination.limit);
            runBackground(fetchLogs(newOffset), 'logs');
        }
    };

    // ============================================================================
    // RENDER HELPERS
    // ============================================================================

    const renderSortIcon = (field: string) => {
        if (sortField !== field) {
            return <ArrowUpDown className="w-3 h-3 ml-1 inline opacity-30" />;
        }
        return sortDirection === 'asc' ?
            <ArrowUp className="w-3 h-3 ml-1 inline text-blue-500" /> :
            <ArrowDown className="w-3 h-3 ml-1 inline text-blue-500" />;
    };

    const getStatusBadge = (success: boolean) => {
        return success ?
            <CheckCircle className="w-3 h-3 text-green-500 inline" /> :
            <XCircle className="w-3 h-3 text-red-500 inline" />;
    };

    // Compliance tag styling. Tags share a
    // common prefix (`gdpr_art_30`, `eu_ai_act.high`, `soc2.cc7.2`,
    // `hipaa.phi`, `iso_42001`). We colour-code by regime so a
    // packed row stays readable, and shorten the display label by
    // stripping the regime prefix where the colour already conveys
    // it (`soc2.cc7.2` → chip "soc2" + label "cc7.2").
    const getComplianceTagStyle = (tag: string): { color: string; label: string; regime: string } => {
        if (tag === 'gdpr_art_30') {
            return { color: 'bg-purple-100 text-purple-800 border-purple-200', label: 'Art 30', regime: 'gdpr' };
        }
        if (tag.startsWith('eu_ai_act.')) {
            return { color: 'bg-blue-100 text-blue-800 border-blue-200', label: tag.slice('eu_ai_act.'.length), regime: 'eu_ai_act' };
        }
        if (tag.startsWith('soc2.')) {
            return { color: 'bg-emerald-100 text-emerald-800 border-emerald-200', label: tag.slice('soc2.'.length), regime: 'soc2' };
        }
        if (tag.startsWith('hipaa.')) {
            return { color: 'bg-rose-100 text-rose-800 border-rose-200', label: tag.slice('hipaa.'.length), regime: 'hipaa' };
        }
        if (tag === 'iso_42001') {
            return { color: 'bg-amber-100 text-amber-800 border-amber-200', label: 'ISO 42001', regime: 'iso_42001' };
        }
        return { color: 'bg-surface-raised text-secondary border-default', label: tag, regime: 'other' };
    };

    /** Render a clickable compliance chip that, when clicked, sets
     *  the table-level filter to that tag (so the operator can drill
     *  from "row carried eu_ai_act.high" → "show me every row that
     *  fired eu_ai_act.high in this window"). */
    const renderComplianceChip = (tag: string) => {
        const { color, label, regime } = getComplianceTagStyle(tag);
        return (
            <button
                type="button"
                key={tag}
                onClick={(e) => {
                    e.stopPropagation();
                    // Drill-down: set the sidebar facet to ONLY this
                    // tag (operator wants a focused view). Keeps the
                    // semantics of the previous string-state version
                    // — one tag selected, not appended.
                    setComplianceTagFilter([tag]);
                }}
                title={`${tag} — click to filter the table`}
                className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded border text-[10px] font-medium leading-none ${color} hover:opacity-80 transition-opacity`}
            >
                <span className="uppercase tracking-wide opacity-70">{regime}</span>
                <span>{label}</span>
            </button>
        );
    };

    // ============================================================================
    // RENDER: FILTER PANEL
    // ============================================================================

    // Slim filter panel — only the non-facetable controls live here
    // (Time Range + Min/Max Duration). Every value-based dimension
    // — Resource, Gateway user, IdP user, Target URL, Capability,
    // Proxy type, Status, Compliance tag — lives in the FacetSidebar
    // so the two surfaces don't duplicate. The Datadog-style sidebar
    // is the single source of truth for value-based filtering; this
    // panel exists only for ranges that can't be enumerated as facet
    // rows.
    const renderFilterPanel = () => (
        <div className={`bg-surface-alt border-b border-default p-3 transition-all duration-200 ${showFilters ? '' : 'hidden'}`}>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs">
                <div>
                    <label className="block text-xs font-medium text-secondary mb-1">
                        Time Range
                    </label>
                    <TimeRangePicker value={timeRange} onChange={setTimeRange} />
                </div>

                <div>
                    <label className="block text-xs font-medium text-secondary mb-1">
                        Min Duration (ms)
                    </label>
                    <input
                        type="number"
                        value={minDuration}
                        onChange={(e) => setMinDuration(e.target.value)}
                        placeholder="Min ms..."
                        className="w-full px-2 py-1 text-xs border border-strong rounded focus:ring-1 focus:ring-blue-500 focus:border-blue-500"
                    />
                </div>

                <div>
                    <label className="block text-xs font-medium text-secondary mb-1">
                        Max Duration (ms)
                    </label>
                    <input
                        type="number"
                        value={maxDuration}
                        onChange={(e) => setMaxDuration(e.target.value)}
                        placeholder="Max ms..."
                        className="w-full px-2 py-1 text-xs border border-strong rounded focus:ring-1 focus:ring-blue-500 focus:border-blue-500"
                    />
                </div>
            </div>

            <div className="mt-3 flex justify-end">
                <button
                    onClick={handleClearFilters}
                    className="px-3 py-1 text-xs text-secondary hover:text-default flex items-center gap-1"
                >
                    <X className="w-3 h-3" />
                    Clear Filters
                </button>
            </div>
        </div>
    );

    // ============================================================================
    // RENDER: TABLE ROW
    // ============================================================================

    const renderRow = (log: UnifiedProxyLogEntry) => {
        const isExpanded = expandedRow === log.id;
        const capability = getCapabilityDisplay(log);
        // A refused credential of a disabled, expired or revoked agent: a
        // row of the surface it was refused on, not of `oauth`.
        const refused = refusedCredentialOf(log);

        // Response-cache operation, surfaced in the Capability column so it's
        // visible in the list (not only in the LLM Details drawer). Derived
        // from existing fields: cache_hit + cache_layer ('exact'|'semantic' on
        // a hit; 'lookup'|'store' on the self-call embedding rows).
        const cacheOp: string | null = (() => {
            const cl = log.llm?.cache_layer;
            if (log.llm?.cache_hit) {
                if (cl === 'exact') return 'exact_cache_hit';
                if (cl === 'semantic') return 'semantic_cache_hit';
                return 'cache_hit';
            }
            // The lookup/store embedding self-calls are both just the
            // prompt being turned into a vector — surface that plainly.
            if (cl === 'lookup' || cl === 'store') return 'prompt_to_embedding';
            return null;
        })();

        return (
            <React.Fragment key={log.id}>
                {/* Main Row */}
                <tr
                    className="border-b border-default hover:bg-surface-alt cursor-pointer transition-colors text-xs"
                    onClick={() => toggleRowExpansion(log.id)}
                >
                    {/* Expand Icon */}
                    <td className="px-2 py-1.5 whitespace-nowrap">
                        {isExpanded ?
                            <ChevronDown className="w-3 h-3 text-muted" /> :
                            <ChevronRight className="w-3 h-3 text-muted" />
                        }
                    </td>

                    {/* Time */}
                    <td className="px-2 py-1.5 whitespace-nowrap text-secondary">
                        {new Date(log.timestamp).toLocaleString('en-US', {
                            month: '2-digit',
                            day: '2-digit',
                            hour: '2-digit',
                            minute: '2-digit',
                            second: '2-digit',
                            hour12: false
                        })}
                    </td>

                    {/* Gateway / IdP User — merged column: the gateway
                        (Brutor) user on top, the upstream IdP identity
                        beneath it in a muted second line. */}
                    <td className="px-2 py-1.5 max-w-[140px] text-secondary">
                        <div className="flex flex-col leading-tight">
                            <span className="truncate" title={log.gateway_user_username || log.gateway_user_id || 'N/A'}>
                                {log.gateway_user_username || (log.gateway_user_id ? (
                                    <span className="font-mono">{log.gateway_user_id.substring(0, 8)}...</span>
                                ) : (
                                    <span className="text-muted">-</span>
                                ))}
                            </span>
                            <span className="truncate text-[11px] text-muted" title={log.idp_user_username || log.idp_user_id || 'N/A'}>
                                {log.idp_user_username || (log.idp_user_id ? `${log.idp_user_id.substring(0, 8)}...` : '-')}
                            </span>
                        </div>
                    </td>

                    {/* Server / Model */}
                    <td className="px-2 py-1.5 max-w-[100px] truncate" title={
                        log.proxy_type === 'llm'
                            ? (log.llm?.model_used || log.llm?.model_requested || log.server_id || 'N/A')
                            : (log.server_id || 'N/A')
                    }>
                        <span className="font-mono text-secondary text-xs">
                            {log.proxy_type === 'llm'
                                ? (log.llm?.model_used || log.llm?.model_requested || (log.server_id ? log.server_id.substring(0, 16) : '-'))
                                : (log.server_id ? log.server_id.substring(0, 16) : '-')
                            }
                        </span>
                        {log.llm?.cache_hit && (
                            <span className="ml-1 text-[10px] px-1 py-0.5 rounded bg-green-100 text-green-700 font-medium">
                                {log.llm.cache_layer || 'cached'}
                            </span>
                        )}
                        {/* Semantic-cache self-call embeddings carry a
                            lookup/store cache_layer (not a cache hit) so the
                            request-side query embed reads apart from the
                            response-side key embed. Tag it distinctly. */}
                        {!log.llm?.cache_hit && (log.llm?.cache_layer === 'lookup' || log.llm?.cache_layer === 'store') && (
                            <span className="ml-1 text-[10px] px-1 py-0.5 rounded bg-indigo-100 text-indigo-700 font-medium">
                                cache:{log.llm.cache_layer}
                            </span>
                        )}
                    </td>

                    {/* HTTP Method — JUST the HTTP verb. The MCP
                        JSON-RPC method (initialize, tools/list,
                        prompts/list, …) is not shown here as
                        `POST (tools/list)`, which would conflate
                        transport with payload. It is in the
                        Capability column. */}
                    <td className="px-2 py-1.5 whitespace-nowrap">
                        <span className="font-medium text-secondary">{log.request?.method || 'N/A'}</span>
                    </td>

                    {/* Target URL — the upstream the gateway
                        forwarded to. Surfaced as its own column so
                        operators can spot "wait, why are we hitting
                        github.com" at a glance, rather than having
                        to expand each row. Monospace + ellipsis so
                        long URLs don't blow up the row height. */}
                    <td
                        className="px-2 py-1.5 max-w-[260px] truncate"
                        title={log.request?.target_url || ''}
                    >
                        {log.request?.target_url ? (
                            <span className="font-mono text-xs text-secondary">
                                {log.request.target_url}
                            </span>
                        ) : (
                            <span className="text-muted">-</span>
                        )}
                    </td>

                    {/* Compliance & Residency — promoted out of the
                        expanded drawer into its own column so the audit /
                        residency posture is visible at a glance. Compliance
                        tags as chips (capped at 3 + "+N"); a compact
                        region / residency-class line beneath. */}
                    <td className="px-2 py-1.5 max-w-[180px]">
                        {(log.compliance_tags && log.compliance_tags.length > 0) || log.region_routed_to || log.residency_class ? (
                            <div className="flex flex-col gap-0.5">
                                {log.compliance_tags && log.compliance_tags.length > 0 && (
                                    <div className="flex flex-wrap items-center gap-0.5">
                                        {log.compliance_tags.slice(0, 3).map((t) => renderComplianceChip(t))}
                                        {log.compliance_tags.length > 3 && (
                                            <span
                                                className="inline-flex items-center px-1 py-0.5 rounded text-[10px] font-medium leading-none bg-surface-raised text-muted border border-default"
                                                title={`+${log.compliance_tags.length - 3} more — expand the row to see all`}
                                            >
                                                +{log.compliance_tags.length - 3}
                                            </span>
                                        )}
                                    </div>
                                )}
                                {(log.region_routed_to || log.residency_class) && (
                                    <div className="flex flex-wrap items-center gap-1 text-[10px] leading-tight">
                                        {log.region_routed_to && (
                                            <span className="font-mono text-muted">{log.region_routed_to}</span>
                                        )}
                                        {log.residency_class && (
                                            <span className={`font-mono ${
                                                log.residency_class === 'blocked' ? 'text-red-600' :
                                                log.residency_class === 'allowed' ? 'text-green-600' :
                                                'text-muted'
                                            }`}>
                                                {log.residency_class}
                                            </span>
                                        )}
                                    </div>
                                )}
                            </div>
                        ) : (
                            <span className="text-muted">-</span>
                        )}
                    </td>

                    {/* Capability — the tool/resource/prompt name a
                        tools-call invoked, the JSON-RPC method for
                        connect/discovery handshakes, or the response-cache
                        operation on an LLM call. All share the mono/blue
                        tool-name styling. */}
                    <td className="px-2 py-1.5 max-w-[140px]" title={capability || cacheOp || ''}>
                        {capability ? (
                            <span className="font-mono text-blue-600 text-xs truncate block">{capability}</span>
                        ) : cacheOp ? (
                            <span
                                className="font-mono text-blue-600 text-xs truncate block"
                                title="Response-cache operation on this LLM call"
                            >
                                {cacheOp}
                            </span>
                        ) : refused ? (
                            <span
                                data-testid="refused-credential-mark"
                                className="text-red-600 text-xs font-medium truncate block"
                                title={refusedCredentialSentence(refused)}
                            >
                                {refusedCredentialMark(refused)}
                            </span>
                        ) : (
                            <span className="text-muted">-</span>
                        )}
                    </td>

                    {/* Duration */}
                    <td className="px-2 py-1.5 whitespace-nowrap text-right">
                        <span className={`font-medium ${
                            log.response?.duration_ms && log.response?.duration_ms > 1000 ? 'text-orange-600' :
                                log.response?.duration_ms && log.response?.duration_ms > 500 ? 'text-yellow-600' :
                                    'text-secondary'
                        }`}>
                            {formatDuration(log.response?.duration_ms)}
                        </span>
                    </td>

                    {/* HTTP Status */}
                    <td className="px-2 py-1.5 whitespace-nowrap text-center">
                        <div className="flex items-center gap-1 justify-center">
                            {getStatusBadge(log.success)}
                            <span className={`font-medium ${
                                log.response?.status_code && log.response.status_code >= 200 && log.response.status_code < 300 ? 'text-green-600' :
                                    log.response?.status_code && log.response.status_code >= 400 ? 'text-red-600' :
                                        'text-secondary'
                            }`}>
                                {log.response?.status_code || 'N/A'}
                            </span>
                        </div>
                    </td>

                    {/* Size (Request -> Response) */}
                    <td className="px-2 py-1.5 whitespace-nowrap text-right text-xs">
                        <div className="flex items-center justify-end gap-1">
                            <span className="text-muted">{formatBytes(log.request?.size_bytes)}</span>
                            <span className="text-muted">→</span>
                            <span className="text-secondary">{formatBytes(log.response?.size_bytes)}</span>
                        </div>
                    </td>
                </tr>

                {/* Expanded Row Details */}
                {isExpanded && (
                    <tr className="bg-surface-alt border-b border-default">
                        <td colSpan={11} className="px-4 py-3">
                            {/* Toolbar — quick actions for the expanded row.
                                "View" opens the full-data audit modal with
                                every field on UnifiedProxyLogEntry pretty-
                                printed (headers, full bodies, MCP / LLM /
                                OAuth blocks, custom metadata). The inline
                                grid below is for triage; the modal is for
                                forensics. */}
                            <div className="flex justify-end gap-2 mb-3">
                                {/* Counters deep-link — when the row
                                    is a rate-limit rejection (429 OR an
                                    enforced_limit_type stamp), surface a
                                    one-click jump to the matching counter
                                    bucket on the System → Counters page.
                                    The Counters page lands scoped + with
                                    the detail drawer auto-opened so the
                                    operator can Reset / Release in two
                                    clicks total. Non-rate-limit rows hide
                                    the button — non-forensic noise. */}
                                {shouldShowCountersLink(
                                    log,
                                ) && (
                                    <CountersDeepLinkButton
                                        input={deepLinkInputFromProxyLogRow(
                                            log,
                                        )}
                                        testId={`proxy-log-counters-link-${log.id}`}
                                        label="View Counters"
                                    />
                                )}
                                {log.root_task_id && (
                                    <button
                                        type="button"
                                        onClick={() => setTraceRootId(log.root_task_id ?? null)}
                                        className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md border border-default bg-surface hover:bg-surface-raised text-default transition-colors"
                                        title="Open the multi-step trace timeline rooted at this call's chain"
                                    >
                                        <GitBranch className="w-3.5 h-3.5" />
                                        View Trace
                                    </button>
                                )}
                                <button
                                    type="button"
                                    onClick={() => setDetailsLog(log)}
                                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md border border-default bg-surface hover:bg-surface-raised text-default transition-colors"
                                    title="Open the full audit-trail view with every recorded field"
                                >
                                    <Eye className="w-3.5 h-3.5" />
                                    View Details
                                </button>
                            </div>
                            {refused && (
                                <div
                                    data-testid="refused-credential-note"
                                    className="mb-3 p-2 rounded border border-red-200 bg-red-50 text-xs text-red-800"
                                >
                                    {refusedCredentialSentence(refused)}
                                </div>
                            )}
                            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 text-xs">
                                {/* Left Column */}
                                <div className="space-y-3">
                                    {/* Agent Identity — actor/subject +
                                        per-agent runtime-authz decision. Shown
                                        only when the request's actor was an
                                        agent principal. */}
                                    {log.actor_agent_id && (
                                        <div>
                                            <div className="text-xs font-semibold text-secondary mb-1">Agent Identity</div>
                                            <div className="bg-surface p-2 rounded border border-default space-y-1">
                                                <div className="flex justify-between">
                                                    <span className="text-muted">Actor (agent)</span>
                                                    <span className="font-mono">{log.actor_agent_id}</span>
                                                </div>
                                                {log.subject_id && (
                                                    <div className="flex justify-between">
                                                        <span className="text-muted">Subject</span>
                                                        <span className="font-mono">{log.subject_kind || 'user'}:{log.subject_id}</span>
                                                    </div>
                                                )}
                                                {log.agent_authz_decision && (
                                                    <div className="flex justify-between">
                                                        <span className="text-muted">Authz decision</span>
                                                        <span className={
                                                            log.agent_authz_decision === 'deny' ? 'text-red-600 font-semibold'
                                                                : log.agent_authz_decision === 'approval_required' ? 'text-yellow-600'
                                                                : 'text-green-700'
                                                        }>{log.agent_authz_decision}{log.agent_grant_id ? ` (grant ${log.agent_grant_id})` : ''}</span>
                                                    </div>
                                                )}
                                            </div>
                                        </div>
                                    )}

                                    {/* Decision record — which policy decided,
                                        under what authority, via which exception path.
                                        Rendered only when a decision was actually taken:
                                        an empty panel would read as "nothing governed
                                        this call", which is a different claim. */}
                                    {(log.guardrail_config_id || log.argument_policy_id
                                        || log.autonomy_level || log.consumed_approval_request_id
                                        || log.pre_action_state || log.deciding_model
                                        || log.tool_definition_hash || log.request_content_hash) && (
                                        <div>
                                            <div className="text-xs font-semibold text-secondary mb-1">Decision Record</div>
                                            <div className="bg-surface p-2 rounded border border-default space-y-1">
                                                {log.guardrail_config_id && (
                                                    <div className="flex justify-between">
                                                        <span className="text-muted">Guardrail</span>
                                                        <span className="font-mono text-right">
                                                            {log.guardrail_check || 'check'}
                                                            {log.guardrail_action ? ` → ${log.guardrail_action}` : ''}
                                                            <span className="text-muted"> ({log.guardrail_config_id})</span>
                                                        </span>
                                                    </div>
                                                )}
                                                {log.argument_policy_id && (
                                                    <div className="flex justify-between">
                                                        <span className="text-muted">Argument policy</span>
                                                        <span className="font-mono">{log.argument_policy_id}</span>
                                                    </div>
                                                )}
                                                {log.autonomy_level && (
                                                    <div className="flex justify-between">
                                                        <span className="text-muted">Autonomy</span>
                                                        <span className={
                                                            log.autonomy_level === 'suspended' ? 'text-red-600 font-semibold'
                                                                : log.autonomy_level === 'restricted' ? 'text-yellow-600'
                                                                : 'font-mono'
                                                        }>
                                                            {log.autonomy_level}
                                                            {/* Provenance is the honesty marker: anything but
                                                                'live' means this was enforced while the
                                                                governance database was unreachable, which the
                                                                decision itself looks identical without. */}
                                                            {log.autonomy_provenance && log.autonomy_provenance !== 'live' && (
                                                                <span
                                                                    className="ml-1 text-orange-600"
                                                                    title="Enforced on degraded information — the governance database was unreachable when this decision was taken."
                                                                >
                                                                    ⚠ {log.autonomy_provenance.replace(/_/g, ' ')}
                                                                </span>
                                                            )}
                                                        </span>
                                                    </div>
                                                )}
                                                {log.consumed_approval_request_id && (
                                                    <div className="flex justify-between">
                                                        <span className="text-muted">Authorised by approval</span>
                                                        <span className="font-mono">{log.consumed_approval_request_id}</span>
                                                    </div>
                                                )}
                                                {log.upstream_request_id && (
                                                    <div className="flex justify-between">
                                                        <span className="text-muted">Upstream request id</span>
                                                        <span className="font-mono break-all">{log.upstream_request_id}</span>
                                                    </div>
                                                )}
                                                {/* Which model chose this action, which
                                                    definition of the tool was in force, and the
                                                    digests that outlive the bodies. */}
                                                {log.deciding_model && (
                                                    <div className="flex justify-between" data-testid="decision-deciding-model">
                                                        <span className="text-muted">Decided by model</span>
                                                        <span className="font-mono">{log.deciding_model}</span>
                                                    </div>
                                                )}
                                                {log.server_version && (
                                                    <div className="flex justify-between">
                                                        <span className="text-muted">Server version</span>
                                                        <span className="font-mono">{log.server_version}</span>
                                                    </div>
                                                )}
                                                {log.tool_definition_hash && (
                                                    <div className="flex justify-between gap-2">
                                                        <span className="text-muted whitespace-nowrap">Tool definition</span>
                                                        <span className="font-mono break-all text-right" title={log.tool_definition_hash}>
                                                            sha256:{log.tool_definition_hash.slice(0, 16)}…
                                                        </span>
                                                    </div>
                                                )}
                                                {log.request_content_hash && (
                                                    <div className="flex justify-between gap-2">
                                                        <span className="text-muted whitespace-nowrap">Request digest</span>
                                                        <span className="font-mono break-all text-right" title={log.request_content_hash}>
                                                            sha256:{log.request_content_hash.slice(0, 16)}…
                                                        </span>
                                                    </div>
                                                )}
                                                {log.response_content_hash && (
                                                    <div className="flex justify-between gap-2">
                                                        <span className="text-muted whitespace-nowrap">Response digest</span>
                                                        <span className="font-mono break-all text-right" title={log.response_content_hash}>
                                                            sha256:{log.response_content_hash.slice(0, 16)}…
                                                        </span>
                                                    </div>
                                                )}
                                                {/* The state the quota decision was taken against.
                                                    Rendered raw: the shape differs per surface, and
                                                    inventing labels for keys we don't control would
                                                    go stale the first time one is added. The limits
                                                    it was judged against live on the AI System's
                                                    contract, not here. */}
                                                {log.pre_action_state && (
                                                    <div className="pt-1 border-t border-default">
                                                        <div className="text-muted mb-0.5">
                                                            State at decision
                                                        </div>
                                                        <pre className="font-mono text-[10px] whitespace-pre-wrap break-all text-secondary">
                                                            {JSON.stringify(log.pre_action_state, null, 1)}
                                                        </pre>
                                                    </div>
                                                )}
                                            </div>
                                        </div>
                                    )}

                                    {/* Target URL */}
                                    <div>
                                        <div className="text-xs font-semibold text-secondary mb-1">Target URL</div>
                                        <div className="font-mono text-xs bg-surface p-2 rounded border border-default break-all">
                                            {log.request?.target_url || 'N/A'}
                                        </div>
                                    </div>

                                    {/* Client IP & Session */}
                                    <div className="grid grid-cols-2 gap-2">
                                        <div>
                                            <div className="text-xs font-semibold text-secondary mb-1">Client IP</div>
                                            <div className="font-mono text-xs bg-surface p-2 rounded border border-default">
                                                {log.client_ip || 'N/A'}
                                            </div>
                                        </div>
                                        <div>
                                            <div className="text-xs font-semibold text-secondary mb-1">Session</div>
                                            {/* Clickable: a session is the whole conversation, so
                                                the useful action on it is "show me the rest of
                                                this chat", not reading the opaque id. Until the
                                                client asserts X-Brutor-Session-Id this equals the
                                                correlation id and the filter returns one row —
                                                correct, if uninteresting. */}
                                            {log.session_id ? (
                                                <button
                                                    type="button"
                                                    onClick={() => setSessionIdFilter(log.session_id)}
                                                    title={`Show every request in session ${log.session_id}`}
                                                    data-testid="session-filter-link"
                                                    className="w-full text-left font-mono text-xs bg-surface p-2 rounded border border-default truncate text-primary-600 hover:underline"
                                                >
                                                    {`${log.session_id.substring(0, 16)}…`}
                                                </button>
                                            ) : (
                                                <div className="font-mono text-xs bg-surface p-2 rounded border border-default">
                                                    N/A
                                                </div>
                                            )}
                                            {log.session_id && (
                                                <button
                                                    type="button"
                                                    disabled={resettingSession === log.session_id}
                                                    onClick={() =>
                                                        void handleResetSessionBudget(log.session_id)
                                                    }
                                                    data-testid="session-budget-reset"
                                                    title={
                                                        'Let this conversation spend again from zero. ' +
                                                        'Its request history is not deleted.'
                                                    }
                                                    className="mt-1 text-xs text-primary-600 hover:underline disabled:opacity-50 disabled:no-underline"
                                                >
                                                    {resettingSession === log.session_id
                                                        ? 'Resetting…'
                                                        : 'Reset spend budget'}
                                                </button>
                                            )}
                                            {resetNotice && (
                                                <div
                                                    className="mt-1 text-xs text-muted"
                                                    data-testid="session-budget-reset-notice"
                                                >
                                                    {resetNotice}
                                                </div>
                                            )}
                                        </div>
                                    </div>

                                    {/* Compliance tags + Feature 7
                                        residency outcome. Three states for compliance_tags:
                                          - undefined → tenant has no compliance profile (engine
                                            never ran); the block is hidden entirely.
                                          - empty []   → profile declared, no tags fired on this
                                            call; render "compliant but quiet" pill.
                                          - [...]      → render colour-coded chips, each
                                            click-to-filter against the table. */}
                                    {(log.compliance_tags !== undefined && log.compliance_tags !== null) || log.residency_class || log.region_routed_to ? (
                                        <div>
                                            <div className="text-xs font-semibold text-secondary mb-1">Compliance &amp; Residency</div>
                                            <div className="bg-surface p-2 rounded border border-default space-y-2">
                                                {log.compliance_tags !== undefined && log.compliance_tags !== null && (
                                                    <div className="flex flex-wrap items-center gap-1">
                                                        {log.compliance_tags.length === 0 ? (
                                                            <span className="text-[10px] text-muted italic">
                                                                Profile declared — no tags fired on this call
                                                            </span>
                                                        ) : (
                                                            log.compliance_tags.map((t) => renderComplianceChip(t))
                                                        )}
                                                    </div>
                                                )}
                                                {(log.residency_class || log.region_routed_to) && (
                                                    <div className="flex flex-wrap items-center gap-2 text-[11px] text-secondary">
                                                        {log.region_routed_to && (
                                                            <span>
                                                                Routed: <span className="font-mono text-default">{log.region_routed_to}</span>
                                                            </span>
                                                        )}
                                                        {log.residency_class && (
                                                            <span>
                                                                Residency:{' '}
                                                                <span className={`font-mono ${
                                                                    log.residency_class === 'blocked' ? 'text-red-600' :
                                                                    log.residency_class === 'allowed' ? 'text-green-600' :
                                                                    'text-muted'
                                                                }`}>
                                                                    {log.residency_class}
                                                                </span>
                                                            </span>
                                                        )}
                                                    </div>
                                                )}
                                            </div>
                                        </div>
                                    ) : null}

                                    {/* Request Preview */}
                                    <div>
                                        <div className="text-xs font-semibold text-secondary mb-1">Request Body</div>
                                        <div className="font-mono text-xs bg-surface p-2 rounded border border-default max-h-32 overflow-y-auto">
                                            <pre className="whitespace-pre-wrap break-words">
                                                {log.request?.body_preview || bodyStateText(log.request?.body_state)}
                                            </pre>
                                        </div>
                                    </div>
                                </div>

                                {/* Right Column */}
                                <div className="space-y-3">
                                    {/* Response Status & Duration */}
                                    <div className="grid grid-cols-2 gap-2">
                                        <div>
                                            <div className="text-xs font-semibold text-secondary mb-1">Response Status</div>
                                            <div className={`text-sm font-bold p-2 rounded border ${
                                                log.success ? 'bg-green-50 border-green-200 text-green-700' : 'bg-red-50 border-red-200 text-red-700'
                                            }`}>
                                                {log.response?.status_code || 'N/A'} - {log.success ? 'Success' : 'Failed'}
                                            </div>
                                        </div>
                                        <div>
                                            <div className="text-xs font-semibold text-secondary mb-1">Duration</div>
                                            <div className="text-sm font-bold bg-surface p-2 rounded border border-default">
                                                {formatDuration(log.response?.duration_ms)}
                                            </div>
                                        </div>
                                    </div>

                                    {/* Response Preview */}
                                    <div>
                                        <div className="text-xs font-semibold text-secondary mb-1">Response Body</div>
                                        <div className="font-mono text-xs bg-surface p-2 rounded border border-default max-h-32 overflow-y-auto">
                                            <pre className="whitespace-pre-wrap break-words">
                                                {log.response?.body_preview || bodyStateText(log.response?.body_state)}
                                            </pre>
                                        </div>
                                    </div>

                                    {/* Error Message (if any) */}
                                    {log.error_message && (
                                        <div>
                                            <div className="text-xs font-semibold text-red-700 mb-1">Error Message</div>
                                            <div className="text-xs bg-red-50 p-2 rounded border border-red-200 text-red-800">
                                                <GatewayRefusalNote text={log.error_message} />
                                                {log.error_message}
                                            </div>
                                        </div>
                                    )}

                                    {/* Active session filter. Rendered as a
                                        dismissible chip rather than another
                                        text input: it is set by clicking a row,
                                        so the thing the operator needs is a way
                                        to SEE it is on and turn it off. */}
                                    {sessionIdFilter && (
                                        <div className="mb-3" data-testid="session-filter-chip">
                                            <div className="text-xs font-semibold text-secondary mb-1">Session</div>
                                            <button
                                                type="button"
                                                onClick={() => setSessionIdFilter('')}
                                                title="Clear the session filter"
                                                className="inline-flex items-center gap-2 px-2 py-1 rounded border border-default bg-surface font-mono text-xs hover:border-strong"
                                            >
                                                <span className="truncate max-w-[14rem]">
                                                    {`${sessionIdFilter.substring(0, 16)}…`}
                                                </span>
                                                <span aria-hidden>×</span>
                                                <span className="sr-only">Clear session filter</span>
                                            </button>
                                        </div>
                                    )}

                                    {/* Correlation IDs */}
                                    {(log.correlation_id || log.parent_request_id) && (
                                        <div>
                                            <div className="text-xs font-semibold text-secondary mb-1">Correlation</div>
                                            <div className="space-y-1 text-xs">
                                                {log.correlation_id && (
                                                    <div className="flex items-start gap-2">
                                                        <span className="text-muted w-16">Corr ID:</span>
                                                        <span className="font-mono text-blue-600 flex-1 break-all min-h-0 min-w-0">{log.correlation_id}</span>
                                                    </div>
                                                )}
                                                {log.parent_request_id && (
                                                    <div className="flex items-start gap-2">
                                                        <span className="text-muted w-16">Parent:</span>
                                                        <span className="font-mono text-blue-600 flex-1 break-all min-h-0 min-w-0">{log.parent_request_id}</span>
                                                    </div>
                                                )}
                                            </div>
                                        </div>
                                    )}
                                    {/* LLM Details (when proxy_type is llm) */}
                                    {log.proxy_type === 'llm' && log.llm && (
                                        <div>
                                            <div className="text-xs font-semibold text-secondary mb-1">LLM Details</div>
                                            <div className="bg-surface p-2 rounded border border-default space-y-1 text-xs">
                                                <div className="grid grid-cols-2 gap-x-4 gap-y-1">
                                                    <div className="flex justify-between">
                                                        <span className="text-muted">Provider</span>
                                                        <span className="font-medium text-default">{log.llm.provider || '-'}</span>
                                                    </div>
                                                    <div className="flex justify-between">
                                                        <span className="text-muted">Model</span>
                                                        <span className="font-mono text-default">{log.llm.model_used || log.llm.model_requested || '-'}</span>
                                                    </div>
                                                    <div className="flex justify-between">
                                                        <span className="text-muted">Prompt Tokens</span>
                                                        <span className="font-medium text-default">{log.llm.prompt_tokens?.toLocaleString() || '-'}</span>
                                                    </div>
                                                    <div className="flex justify-between">
                                                        <span className="text-muted">Completion Tokens</span>
                                                        <span className="font-medium text-default">{log.llm.completion_tokens?.toLocaleString() || '-'}</span>
                                                    </div>
                                                    <div className="flex justify-between">
                                                        <span className="text-muted">Total Tokens</span>
                                                        <span className="font-medium text-default">{log.llm.total_tokens?.toLocaleString() || '-'}</span>
                                                    </div>
                                                    <div className="flex justify-between">
                                                        <span className="text-muted">Est. Cost</span>
                                                        <span className="font-medium text-green-700">
                                                            {log.llm.estimated_cost_usd != null ? `$${log.llm.estimated_cost_usd.toFixed(6)}` : '-'}
                                                        </span>
                                                    </div>
                                                    <div className="flex justify-between">
                                                        <span className="text-muted">Streaming</span>
                                                        <span className="font-medium text-default">{log.llm.streaming ? 'Yes' : 'No'}</span>
                                                    </div>
                                                    <div className="flex justify-between">
                                                        <span className="text-muted">Temperature</span>
                                                        <span className="font-medium text-default">{log.llm.temperature ?? '-'}</span>
                                                    </div>
                                                    {log.llm.used_fallback && (
                                                        <div className="flex justify-between col-span-2">
                                                            <span className="text-muted">Fallback</span>
                                                            <span className="font-medium text-orange-600">{log.llm.fallback_reason || 'Yes'}</span>
                                                        </div>
                                                    )}
                                                </div>

                                                {/* Cache info */}
                                                {log.llm.cache_hit && (
                                                    <div className="mt-2 pt-2 border-t border-default">
                                                        <div className="flex items-center gap-2 mb-1">
                                                            <span className="text-xs font-semibold text-green-700">Cache Hit</span>
                                                            <span className="text-xs px-1.5 py-0.5 rounded-full bg-green-100 text-green-700 font-medium">
                                                                {log.llm.cache_layer || 'hit'}
                                                            </span>
                                                        </div>
                                                        <div className="grid grid-cols-2 gap-x-4 gap-y-1">
                                                            <div className="flex justify-between">
                                                                <span className="text-muted">Tokens Saved</span>
                                                                <span className="font-medium text-green-700">{log.llm.tokens_saved?.toLocaleString() || '-'}</span>
                                                            </div>
                                                            {log.llm.cache_similarity_score != null && (
                                                                <div className="flex justify-between">
                                                                    <span className="text-muted">Similarity</span>
                                                                    <span className="font-medium text-default">{(log.llm.cache_similarity_score * 100).toFixed(1)}%</span>
                                                                </div>
                                                            )}
                                                        </div>
                                                    </div>
                                                )}
                                            </div>
                                        </div>
                                    )}
                                </div>
                            </div>
                        </td>
                    </tr>
                )}
            </React.Fragment>
        );
    };

    // ============================================================================
    // MAIN RENDER
    // ============================================================================

    return (
        <div className="h-full app-column bg-surface">
            {/* Header */}
            <div className="flex items-center justify-between px-4 py-3 border-b border-default bg-surface">
                <div>
                    <h2 className="text-lg font-semibold text-default">Proxy Logs</h2>
                    <p className="text-xs text-muted mt-0.5">
                        {pagination.total.toLocaleString()} total logs
                        {proxyTypesFilter.length > 0 && ` • Filtered by: ${proxyTypesFilter.join(', ')}`}
                    </p>
                </div>

                <div className="flex items-center gap-2">
                    <SavedViewMenu
                        scope="proxy_logs"
                        currentFilters={currentFiltersForSave}
                        onApply={handleApplySavedView}
                        currentAdminId={currentAdminId}
                    />

                    <button
                        onClick={() => setShowFilters(!showFilters)}
                        className={`px-3 py-1.5 text-xs flex items-center gap-2 rounded transition-colors ${
                            showFilters
                                ? 'bg-blue-50 text-blue-700 border border-blue-200'
                                : 'bg-surface-raised text-secondary hover:bg-surface-inset border border-strong'
                        }`}
                    >
                        <Filter className="w-3 h-3" />
                        {showFilters ? 'Hide' : 'Show'} Filters
                    </button>

                    <button
                        onClick={handleRefresh}
                        disabled={loading}
                        className="px-3 py-1.5 text-xs bg-surface border border-strong text-secondary rounded hover:bg-surface-alt disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2 transition-colors"
                    >
                        <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin' : ''}`} />
                        Refresh
                    </button>

                    <button
                        onClick={asyncHandler(() => handleExport('csv'), 'click')}
                        aria-label="Download as CSV"
                        className="p-2 text-secondary hover:text-default hover:bg-surface-raised rounded-md transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                        title="Download as CSV"
                    >
                        <Download className="w-4 h-4" />
                    </button>
                    <button
                        onClick={asyncHandler(() => handleExport('jsonl'), 'click')}
                        className="px-2 py-1 text-xs font-medium text-secondary hover:text-default hover:bg-surface-raised rounded-md transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                        title="Download as JSON Lines"
                    >
                        JSONL
                    </button>
                </div>
            </div>
            {/* Datadog-style two-column layout — FacetSidebar on the
                left, the existing search/filter/table column on the
                right. Both columns share the parent's vertical
                space and scroll independently. */}
            <div className="flex flex-1 min-h-0 min-w-0">
                <FacetSidebar
                    fields={sidebarFields}
                    selected={sidebarSelected}
                    onChange={handleSidebarChange}
                    loadFacets={loadFacets}
                    storageKey="brutor.proxylogs.facetsidebar"
                    refetchKey={sidebarRefetchKey}
                    labels={displayLabels}
                    width={facetWidth}
                />
                {/* Drag splitter — shrink the facet rail to give the log
                    table more room. */}
                <div
                    onMouseDown={startResize('facet')}
                    title="Drag to resize the facet panel"
                    className="w-1 flex-shrink-0 cursor-col-resize bg-default/30 hover:bg-blue-400 transition-colors"
                />
                <div className="flex flex-col flex-1 min-w-0 min-h-0">
            {/* Free-text search bar. The input holds the raw value; only
                `debouncedFreeTextSearch` reaches the filter envelope, because
                a React effect debounces nothing on its own and this box would
                otherwise issue one tsvector scan per keystroke. Matches across
                request/response body previews + target URL + error message via
                a GIN index. */}
            <div className="border-b border-default bg-surface px-3 py-2 flex items-center gap-2">
                <Search className="w-4 h-4 text-muted flex-shrink-0" />
                <input
                    type="text"
                    value={freeTextSearch}
                    onChange={(e) => setFreeTextSearch(e.target.value)}
                    placeholder="Search across body previews, target URL, error messages..."
                    className="flex-1 px-2 py-1 text-xs bg-transparent border-none focus:outline-none min-h-0 min-w-0"
                />
                {freeTextSearch && (
                    <button
                        type="button"
                        onClick={() => setFreeTextSearch('')}
                        className="text-[11px] text-muted hover:text-default"
                    >
                        Clear
                    </button>
                )}
            </div>

            {/* Active filter chips — populated when any faceted
                filter has values. Click × on a chip to drop that
                value; "Clear all" wipes the whole row. */}
            <ActiveFilterChips
                filters={{
                    server_id: serverFilter,
                    gateway_user_id: gatewayUserFilter,
                    idp_user_username: idpUserFilter,
                    target_url: targetUrlFilter,
                    mcp_capability_name: capabilityFilter,
                    proxy_type: proxyTypesFilter,
                    compliance_tags: complianceTagFilter,
                    success: successFilter,
                }}
                fieldLabels={{
                    server_id: 'Resource',
                    gateway_user_id: 'Gateway user',
                    idp_user_username: 'IdP user',
                    target_url: 'Target URL',
                    mcp_capability_name: 'Capability',
                    proxy_type: 'Proxy type',
                    compliance_tags: 'Compliance',
                    success: 'Status',
                }}
                valueLabels={displayLabels}
                onRemove={handleRemoveFilter}
                onClearAll={handleClearFilters}
            />

            {/* Filters */}
            {renderFilterPanel()}

            {/* Error Message */}
            {error && (
                <div className="mx-4 mt-3 p-3 bg-red-50 border border-red-200 rounded-lg">
                    <div className="flex items-start gap-2">
                        <AlertTriangle className="w-4 h-4 text-red-600 mt-0.5 flex-shrink-0" />
                        <div>
                            <div className="text-sm font-medium text-red-800">Error</div>
                            <div className="text-xs text-red-700 mt-1">{error}</div>
                        </div>
                    </div>
                </div>
            )}

            {/* Loading State */}
            {loading && logs.length === 0 && (
                <div className="flex-1 flex items-center justify-center min-h-0 min-w-0">
                    <div className="text-center">
                        <Loader className="w-8 h-8 text-blue-600 animate-spin mx-auto mb-3" />
                        <p className="text-sm text-secondary">Loading logs...</p>
                    </div>
                </div>
            )}

            {/* Table */}
            {!loading && logs.length === 0 && (
                <div className="flex-1 flex items-center justify-center min-h-0 min-w-0">
                    <div className="text-center">
                        <AlertTriangle className="w-12 h-12 text-muted mx-auto mb-3" />
                        <p className="text-sm text-secondary">No logs found</p>
                        <p className="text-xs text-muted mt-1">Try adjusting your filters</p>
                    </div>
                </div>
            )}

            {logs.length > 0 && (
                <div className="flex-1 overflow-auto min-h-0 min-w-0">
                    <table className="text-xs table-fixed" style={{ width: colWidths.reduce((a, b) => a + b, 0) }}>
                        {/* colgroup drives the fixed column widths for both
                            header and body; the per-header drag handles below
                            update colWidths. */}
                        <colgroup>
                            {colWidths.map((w, i) => (
                                <col key={i} style={{ width: w }} />
                            ))}
                        </colgroup>
                        <thead className="bg-surface-alt border-b border-default sticky top-0 z-10">
                        <tr>
                            <th className="px-2 py-2 text-left"></th>
                            <th
                                className={`relative px-2 py-2 text-left cursor-pointer hover:bg-surface-raised ${TABLE_TH_TYPE}`}
                                onClick={() => handleSort('timestamp')}
                            >
                                <div className="flex items-center gap-1">
                                    Time
                                    {renderSortIcon('timestamp')}
                                </div>
                                <span onMouseDown={startResize(1)} className="absolute top-0 right-0 z-10 h-full w-1 cursor-col-resize hover:bg-blue-400" />
                            </th>
                            <th className={`relative px-2 py-2 text-left ${TABLE_TH_TYPE}`} title="Authenticated identity: gateway user or group-scoped key (top), federated IdP identity (below)">Principal
                                <span onMouseDown={startResize(2)} className="absolute top-0 right-0 z-10 h-full w-1 cursor-col-resize hover:bg-blue-400" />
                            </th>
                            <th className={`relative px-2 py-2 text-left ${TABLE_TH_TYPE}`}>Resource
                                <span onMouseDown={startResize(3)} className="absolute top-0 right-0 z-10 h-full w-1 cursor-col-resize hover:bg-blue-400" />
                            </th>
                            <th className={`relative px-2 py-2 text-left ${TABLE_TH_TYPE}`}>Method
                                <span onMouseDown={startResize(4)} className="absolute top-0 right-0 z-10 h-full w-1 cursor-col-resize hover:bg-blue-400" />
                            </th>
                            <th className={`relative px-2 py-2 text-left ${TABLE_TH_TYPE}`}>Target
                                <span onMouseDown={startResize(5)} className="absolute top-0 right-0 z-10 h-full w-1 cursor-col-resize hover:bg-blue-400" />
                            </th>
                            <th className={`relative px-2 py-2 text-left ${TABLE_TH_TYPE}`}>Compliance &amp; Residency
                                <span onMouseDown={startResize(6)} className="absolute top-0 right-0 z-10 h-full w-1 cursor-col-resize hover:bg-blue-400" />
                            </th>
                            <th className={`relative px-2 py-2 text-left ${TABLE_TH_TYPE}`}>Capability
                                <span onMouseDown={startResize(7)} className="absolute top-0 right-0 z-10 h-full w-1 cursor-col-resize hover:bg-blue-400" />
                            </th>
                            <th
                                className={`relative px-2 py-2 text-right cursor-pointer hover:bg-surface-raised ${TABLE_TH_TYPE}`}
                                onClick={() => handleSort('duration_ms')}
                            >
                                <div className="flex items-center justify-end gap-1">
                                    Duration
                                    {renderSortIcon('duration_ms')}
                                </div>
                                <span onMouseDown={startResize(8)} className="absolute top-0 right-0 z-10 h-full w-1 cursor-col-resize hover:bg-blue-400" />
                            </th>
                            <th className={`relative px-2 py-2 text-center ${TABLE_TH_TYPE}`}>Status
                                <span onMouseDown={startResize(9)} className="absolute top-0 right-0 z-10 h-full w-1 cursor-col-resize hover:bg-blue-400" />
                            </th>
                            <th className={`px-2 py-2 text-right ${TABLE_TH_TYPE}`}>Size</th>
                        </tr>
                        </thead>
                        <tbody>
                        {logs.map(log => renderRow(log))}
                        </tbody>
                    </table>
                </div>
            )}

            {/* Pagination */}
            {logs.length > 0 && (
                <div className="flex items-center justify-between px-4 py-3 border-t border-default bg-surface">
                    <div className="text-xs text-secondary">
                        Showing {pagination.offset + 1} - {pagination.offset + pagination.returned} of {pagination.total.toLocaleString()}
                    </div>

                    <div className="flex items-center gap-2">
                        <button
                            onClick={handlePrevPage}
                            disabled={pagination.offset === 0 || loading}
                            className="px-3 py-1 text-xs bg-surface border border-strong text-secondary rounded hover:bg-surface-alt disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                        >
                            Previous
                        </button>
                        <button
                            onClick={handleNextPage}
                            disabled={!pagination.has_more || loading}
                            className="px-3 py-1 text-xs bg-surface border border-strong text-secondary rounded hover:bg-surface-alt disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                        >
                            Next
                        </button>
                    </div>
                </div>
            )}
                </div>{/* /right column */}
            </div>{/* /horizontal flex row containing sidebar + right column */}

            {/* Full-data audit modal, mounted at the viewer root so
                it overlays the table without being clipped by the
                row's own DOM. */}
            {detailsLog && (
                <ProxyLogDetailsModal
                    log={detailsLog}
                    onClose={() => setDetailsLog(null)}
                />
            )}
            {traceRootId && (
                <TraceTimelineModal
                    rootTaskId={traceRootId}
                    onClose={() => setTraceRootId(null)}
                />
            )}
        </div>
    );
};

export default ProxyLogsViewer;
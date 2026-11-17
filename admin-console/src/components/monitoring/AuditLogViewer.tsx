/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * System Audit Log Viewer with Table Display
 */

import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import {
    Search,
    Download,
    RefreshCw,
    Pause,
    Play,
    AlertCircle,
    X,
    CheckCircle,
    Filter,
    ChevronDown,
    ChevronRight
} from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import { TABLE_TH, TABLE_THEAD } from '../ui/tableStyles';
import { ActiveFilterChips } from './FacetMultiSelect';
import FacetSidebar from './FacetSidebar';
import SavedViewMenu from './SavedViewMenu';
import { useAuth } from '../../hooks/useAuth';
import TimeRangePicker, { TimeRange } from './TimeRangePicker';
import { timeRangeToBody } from './timeRangeToBody';
import { auditLogExportParams } from './auditLogExport';
import {
    readFiltersFromUrl,
    useUrlFilterSync,
    encodeStringArray, decodeStringArray,
    encodeString, decodeString,
    encodeTimeRange, decodeTimeRange,
} from './useUrlFilterState';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';
import { errorKind } from '../../lib/narrow';

interface AuditLog {
    id: string;
    admin_id: string | null;
    action: string;
    resource_type: string | null;
    resource_id: string | null;
    ip_address: string | null;
    user_agent: string | null;
    request_method: string | null;
    request_path: string | null;
    success: boolean;
    details: Record<string, any> | null;
    timestamp: string;
}

interface LogViewerProps {
    isLive?: boolean;
    onToggleLive?: () => void;
    onRefresh?: () => void;
    onDownload?: () => void;
}

const AuditLogViewer: React.FC<LogViewerProps> = ({
                                                      isLive = true,
                                                      onToggleLive,
                                                      onRefresh,
                                                      onDownload
                                                  }) => {
    const [logs, setLogs] = useState<AuditLog[]>([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // Filters — Datadog-style faceted multi-select. Each holds the
    // operator's currently-selected values; empty array = no filter.
    const [searchTerm, setSearchTerm] = useState('');
    // Only the settled value reaches the filter envelope — the raw state drives
    // the input. Without the split each keystroke is a full /audit-logs/search
    // round trip, and any of those in-flight prefixes can land last.
    const debouncedSearchTerm = useDebouncedValue(searchTerm, 300);
    const [adminIdFilter, setAdminIdFilter] = useState<string[]>([]);
    const [actionFilter, setActionFilter] = useState<string[]>([]);
    const [resourceTypeFilter, setResourceTypeFilter] = useState<string[]>([]);
    const [ipAddressFilter, setIpAddressFilter] = useState<string[]>([]);
    const [requestMethodFilter, setRequestMethodFilter] = useState<string[]>([]);
    // Status is a sidebar facet (audit_logs.success bool). Empty
    // / both = no filter; ['true'] = success only; ['false'] = failure.
    const [successFilter, setSuccessFilter] = useState<string[]>([]);
    const [timeRange, setTimeRange] = useState<TimeRange>({ kind: 'preset', minutes: 60 * 24 });
    const [limit, setLimit] = useState(100);

    // UI state
    const [expandedRow, setExpandedRow] = useState<string | null>(null);
    const [showFilters, setShowFilters] = useState(false);

    const { exportAuditLogs, gatewayClient } = useGateway();

    /**
     * Build the request body for /audit-logs/search + /facets + /suggest.
     * `success` is encoded as a tristate (true / false / undefined) so the
     * backend can apply the equality filter only when the operator
     * explicitly narrowed.
     */
    const buildAuditEnvelope = useCallback(() => ({
        ...timeRangeToBody(timeRange),
        admin_ids: adminIdFilter.length > 0 ? adminIdFilter : undefined,
        actions: actionFilter.length > 0 ? actionFilter : undefined,
        resource_types: resourceTypeFilter.length > 0 ? resourceTypeFilter : undefined,
        ip_addresses: ipAddressFilter.length > 0 ? ipAddressFilter : undefined,
        request_methods: requestMethodFilter.length > 0 ? requestMethodFilter : undefined,
        success: successFilter.length === 1
            ? successFilter[0] === 'true'
            : undefined,
        q: debouncedSearchTerm || undefined,
    }), [
        timeRange, adminIdFilter, actionFilter, resourceTypeFilter,
        ipAddressFilter, requestMethodFilter, successFilter, debouncedSearchTerm,
    ]);

    /**
     * Facet + suggest loaders for each FacetMultiSelect — wrap the
     * client methods with the active filter envelope so the dropdown
     * values reflect the current scope. Same pattern as ProxyLogsViewer.
     */
    const loadFacets = useCallback(async (field: string) => {
        if (!gatewayClient) return [];
        const body = { ...buildAuditEnvelope(), fields: [field], per_field_limit: 50 };
        const res = await gatewayClient.fetchAdminAuditFacets(body);
        return (res.facets?.[field] ?? []).map(r => ({ value: r.value, count: r.count }));
    }, [gatewayClient, buildAuditEnvelope]);

    // Fetch audit logs — POST /audit-logs/search accepts the same
    // multi-value envelope as /facets so the table and the facet
    // dropdowns always match.
    const loadSeqRef = useRef(0);

    const loadAuditLogs = useCallback(async () => {
        if (!gatewayClient) return;

        // Monotonic sequence — see ProxyLogsViewer. Responses do not arrive in
        // issue order, and the 10s live poll keeps a second request in flight,
        // so a slow one for the previous filter would otherwise land last and
        // repaint the table with rows the chips no longer describe.
        const seq = ++loadSeqRef.current;

        setLoading(true);
        setError(null);

        try {
            const body = { ...buildAuditEnvelope(), limit };
            const response = await gatewayClient.searchAdminAuditLogs(body);
            if (seq !== loadSeqRef.current) return;
            setLogs(response.logs || []);
        } catch (err) {
            if (seq !== loadSeqRef.current) return;
            console.error('Failed to fetch audit logs:', errorKind(err));
            setError(err instanceof Error ? err.message : 'Failed to load audit logs');
        } finally {
            // A superseded request must not clear the spinner the newer one set.
            if (seq === loadSeqRef.current) setLoading(false);
        }
    }, [gatewayClient, buildAuditEnvelope, limit]);


    // URL hydration — read ?a_… query params on mount and apply to
    // local state. Uses a different prefix from ProxyLogsViewer's
    // 'p_' so the two viewers can share a URL without colliding.
    useEffect(() => {
        const fromUrl = readFiltersFromUrl('a_', {
            admins:     decodeStringArray,
            actions:    decodeStringArray,
            resources:  decodeStringArray,
            ips:        decodeStringArray,
            methods:    decodeStringArray,
            status:     decodeStringArray,
            q:          decodeString,
            time:       decodeTimeRange(60 * 24),
        });
        if (fromUrl) {
            setAdminIdFilter(fromUrl.admins);
            setActionFilter(fromUrl.actions);
            setResourceTypeFilter(fromUrl.resources);
            setIpAddressFilter(fromUrl.ips);
            setRequestMethodFilter(fromUrl.methods);
            setSuccessFilter(fromUrl.status);
            setSearchTerm(fromUrl.q);
            setTimeRange(fromUrl.time);
        }
         
    }, []);

    // URL sync — write filters back to URL on change. Wraps
    // history.replaceState so typing in the search box doesn't spam
    // history entries.
    useUrlFilterSync(
        'a_',
        {
            admins:    adminIdFilter,
            actions:   actionFilter,
            resources: resourceTypeFilter,
            ips:       ipAddressFilter,
            methods:   requestMethodFilter,
            status:    successFilter,
            q:         searchTerm,
            time:      timeRange,
        },
        {
            admins:    encodeStringArray,
            actions:   encodeStringArray,
            resources: encodeStringArray,
            ips:       encodeStringArray,
            methods:   encodeStringArray,
            status:    encodeStringArray,
            q:         encodeString,
            time:      encodeTimeRange(60 * 24),
        },
    );

    // Initial load
    useEffect(() => {
        runBackground(loadAuditLogs(), 'audit logs');
    }, [loadAuditLogs]);

    // Auto-refresh in live mode
    useEffect(() => {
        if (isLive) {
            const interval = setInterval(() => runBackground(loadAuditLogs(), 'audit logs'), 10000); // Refresh every 10 seconds
            return () => clearInterval(interval);
        }
    }, [isLive, loadAuditLogs]);

    // Filtering is server-side, so `logs` is already the filtered set. A
    // mirrored `filteredLogs` state would add a state write and a second
    // render pass on every load and every 10s live tick, and an "N of N
    // logs" readout could only ever show the same number twice.
    // Memoised because the footer counts the array twice per render.
    const successCount = useMemo(
        () => logs.filter(log => log.success).length,
        [logs],
    );

    const handleRefresh = () => {
        runBackground(loadAuditLogs(), 'audit logs');
        if (onRefresh) onRefresh();
    };

    // The export takes the filters of the list in full, so the file holds
    // the rows the list shows.
    const handleDownload = async (format: 'csv' | 'jsonl' = 'csv') => {
        const params = auditLogExportParams(buildAuditEnvelope());
        try {
            const { blob, filename } = await exportAuditLogs(params, format);
            const fallback = `audit-logs-${new Date().toISOString().replace(/[:.]/g, '-')}.${format}`;
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = filename || fallback;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);

            if (onDownload) onDownload();
        } catch (err) {
            console.error('Failed to download logs:', errorKind(err));
            setError(err instanceof Error ? err.message : 'Failed to export audit logs');
        }
    };

    const toggleRowExpansion = (id: string) => {
        setExpandedRow(expandedRow === id ? null : id);
    };

    const formatTimestamp = (timestamp: string) => {
        const date = new Date(timestamp);
        return date.toLocaleString([], {
            year: 'numeric',
            month: 'short',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit'
        });
    };

    const getActionBadgeColor = (action: string | undefined | null): string => {
        if (!action) return 'bg-surface-raised text-default';

        const lowerAction = action.toLowerCase();
        if (lowerAction.includes('create') || lowerAction.includes('register')) return 'bg-green-100 text-green-800';
        if (lowerAction.includes('update') || lowerAction.includes('modify')) return 'bg-blue-100 text-blue-800';
        if (lowerAction.includes('delete') || lowerAction.includes('remove')) return 'bg-red-100 text-red-800';
        if (lowerAction.includes('login') || lowerAction.includes('auth')) return 'bg-purple-100 text-purple-800';
        if (lowerAction.includes('read') || lowerAction.includes('view') || lowerAction.includes('get')) return 'bg-surface-raised text-default';
        return 'bg-yellow-100 text-yellow-800';
    };

    const getStatusIcon = (success: boolean) => {
        return success ? (
            <CheckCircle className="w-4 h-4 text-green-600" />
        ) : (
            <X className="w-4 h-4 text-red-600" />
        );
    };

    /**
     * Remove a single (field, value) pair — used by the
     * ActiveFilterChips × button. Same dispatch shape as
     * ProxyLogsViewer.handleRemoveFilter.
     */
    const handleRemoveFilter = (field: string, value: string) => {
        switch (field) {
            case 'admin_id':
                setAdminIdFilter((prev) => prev.filter((v) => v !== value));
                break;
            case 'action':
                setActionFilter((prev) => prev.filter((v) => v !== value));
                break;
            case 'resource_type':
                setResourceTypeFilter((prev) => prev.filter((v) => v !== value));
                break;
            case 'ip_address':
                setIpAddressFilter((prev) => prev.filter((v) => v !== value));
                break;
            case 'request_method':
                setRequestMethodFilter((prev) => prev.filter((v) => v !== value));
                break;
            case 'success':
                setSuccessFilter((prev) => prev.filter((v) => v !== value));
                break;
            default:
                break;
        }
    };

    const handleClearFilters = () => {
        setAdminIdFilter([]);
        setActionFilter([]);
        setResourceTypeFilter([]);
        setIpAddressFilter([]);
        setRequestMethodFilter([]);
        setSuccessFilter([]);
        setSearchTerm('');
        setTimeRange({ kind: 'preset', minutes: 60 * 24 });
    };

    /**
     * Apply a saved view's stored envelope to local state. Forward-
     * compatible with envelopes that grow new keys — unknown ones are
     * silently dropped instead of breaking the apply.
     */
    const handleApplySavedView = useCallback((filters: Record<string, any>) => {
        setAdminIdFilter(Array.isArray(filters.admin_ids) ? filters.admin_ids : []);
        setActionFilter(Array.isArray(filters.actions) ? filters.actions : []);
        setResourceTypeFilter(Array.isArray(filters.resource_types) ? filters.resource_types : []);
        setIpAddressFilter(Array.isArray(filters.ip_addresses) ? filters.ip_addresses : []);
        setRequestMethodFilter(Array.isArray(filters.request_methods) ? filters.request_methods : []);
        setSuccessFilter(
            filters.success === true ? ['true']
            : filters.success === false ? ['false']
            : [],
        );
        setSearchTerm(typeof filters.q === 'string' ? filters.q : '');
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
    const currentFiltersForSave = useMemo(
        () => buildAuditEnvelope(),
        [buildAuditEnvelope],
    );

    // --- FacetSidebar wiring -------------------------------------------------
    // Order mirrors ProxyLogsViewer's sidebar so muscle memory carries
    // between the two viewers. `storageKey` is namespaced 'audit_logs'
    // so the operator's collapse state doesn't collide with the proxy
    // viewer's.
    const sidebarFields = useMemo(() => ([
        { field: 'admin_id',       label: 'Admin' },
        { field: 'action',         label: 'Action' },
        { field: 'resource_type',  label: 'Resource type' },
        { field: 'ip_address',     label: 'IP address' },
        { field: 'request_method', label: 'HTTP method' },
        { field: 'success',        label: 'Status' },
    ]), []);

    const sidebarSelected = useMemo(() => ({
        admin_id: adminIdFilter,
        action: actionFilter,
        resource_type: resourceTypeFilter,
        ip_address: ipAddressFilter,
        request_method: requestMethodFilter,
        success: successFilter,
    }), [adminIdFilter, actionFilter, resourceTypeFilter, ipAddressFilter, requestMethodFilter, successFilter]);

    const handleSidebarChange = useCallback((field: string, next: string[]) => {
        switch (field) {
            case 'admin_id':
                setAdminIdFilter(next);
                break;
            case 'action':
                setActionFilter(next);
                break;
            case 'resource_type':
                setResourceTypeFilter(next);
                break;
            case 'ip_address':
                setIpAddressFilter(next);
                break;
            case 'request_method':
                setRequestMethodFilter(next);
                break;
            case 'success':
                setSuccessFilter(next);
                break;
            default:
                break;
        }
    }, []);

    // Refetch facet counts whenever any sibling filter changes —
    // open sections re-load via the JSON-stringified envelope hash.
    const sidebarRefetchKey = useMemo(
        () => JSON.stringify(buildAuditEnvelope()),
        [buildAuditEnvelope],
    );

    return (
        <div className="bg-surface rounded-lg border h-full app-column">
            {/* Header */}
            <div className="px-6 py-4 border-b">
                <div className="flex items-center justify-between">
                    <div className="flex items-center space-x-4">
                        <h3 className="text-lg font-semibold text-default">Admin Audit Logs</h3>
                        <div className="flex items-center space-x-2">
                            <button
                                onClick={onToggleLive || (() => {})}
                                className={`flex items-center space-x-1 px-3 py-1 rounded-md text-sm font-medium transition-colors ${
                                    isLive
                                        ? 'bg-green-100 text-green-700 hover:bg-green-200'
                                        : 'bg-surface-raised text-secondary hover:bg-surface-inset'
                                }`}
                            >
                                {isLive ? <Pause className="w-3 h-3" /> : <Play className="w-3 h-3" />}
                                <span>{isLive ? 'Live' : 'Paused'}</span>
                            </button>
                            <span className="text-sm text-muted">
                {logs.length} logs
              </span>
                        </div>
                    </div>

                    <div className="flex items-center space-x-2">
                        <SavedViewMenu
                            scope="admin_audit_logs"
                            currentFilters={currentFiltersForSave}
                            onApply={handleApplySavedView}
                            currentAdminId={currentAdminId}
                        />
                        <button
                            onClick={() => setShowFilters(!showFilters)}
                            className={`p-2 rounded-md transition-colors ${
                                showFilters ? 'bg-primary-100 text-primary' : 'text-secondary hover:text-default hover:bg-surface-raised'
                            }`}
                            title="Toggle filters"
                        >
                            <Filter className="w-4 h-4" />
                        </button>
                        <button
                            onClick={handleRefresh}
                            disabled={loading}
                            className="p-2 text-secondary hover:text-default hover:bg-surface-raised rounded-md transition-colors disabled:opacity-50"
                            title="Refresh logs"
                        >
                            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
                        </button>
                        <button
                            onClick={asyncHandler(() => handleDownload('csv'), 'click')}
                            aria-label="Download as CSV"
                            className="p-2 text-secondary hover:text-default hover:bg-surface-raised rounded-md transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                            title="Download as CSV"
                        >
                            <Download className="w-4 h-4" />
                        </button>
                        <button
                            onClick={asyncHandler(() => handleDownload('jsonl'), 'click')}
                            className="px-2 py-1 text-xs font-medium text-secondary hover:text-default hover:bg-surface-raised rounded-md transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                            title="Download as JSON Lines"
                        >
                            JSONL
                        </button>
                    </div>
                </div>
            </div>

            {/* Horizontal flex row: FacetSidebar on the left, the
                rest of the viewer (search, filters, table, footer)
                on the right. min-h-0 lets the inner table own its
                own scroll without breaking flex sizing. */}
            <div className="flex flex-1 min-h-0 min-w-0">
                <FacetSidebar
                    fields={sidebarFields}
                    selected={sidebarSelected}
                    onChange={handleSidebarChange}
                    labels={{ 'true': 'Success', 'false': 'Failure' }}
                    loadFacets={loadFacets}
                    storageKey="brutor.audit_logs.facetSidebar"
                    refetchKey={sidebarRefetchKey}
                />

                <div className="flex flex-col flex-1 min-w-0 min-h-0">

            {/* Free-text search bar — always visible above the panel
                (matches ProxyLogsViewer). Hits action / resource_type /
                resource_id / ip_address / request_path server-side. */}
            <div className="border-b border-default bg-surface px-3 py-2 flex items-center gap-2">
                <Search className="w-4 h-4 text-muted flex-shrink-0" />
                <input
                    type="text"
                    value={searchTerm}
                    onChange={(e) => setSearchTerm(e.target.value)}
                    placeholder="Search across action, admin, resource, IP, request path..."
                    className="flex-1 px-2 py-1 text-xs bg-transparent border-none focus:outline-none min-h-0 min-w-0"
                />
                {searchTerm && (
                    <button
                        type="button"
                        onClick={() => setSearchTerm('')}
                        className="text-[11px] text-muted hover:text-default"
                    >
                        Clear
                    </button>
                )}
            </div>

            {/* Active filter chips. Hidden when nothing is selected. */}
            <ActiveFilterChips
                filters={{
                    admin_id: adminIdFilter,
                    action: actionFilter,
                    resource_type: resourceTypeFilter,
                    ip_address: ipAddressFilter,
                    request_method: requestMethodFilter,
                    success: successFilter,
                }}
                fieldLabels={{
                    admin_id: 'Admin',
                    action: 'Action',
                    resource_type: 'Resource type',
                    ip_address: 'IP address',
                    request_method: 'HTTP method',
                    success: 'Status',
                }}
                valueLabels={{ 'true': 'Success', 'false': 'Failure' }}
                onRemove={handleRemoveFilter}
                onClearAll={handleClearFilters}
            />

            {/* Slim filter panel — only non-facetable controls.
                Admin / Action / Resource type / IP address / HTTP
                method / Status are all sidebar facets; this panel
                holds Time Range + Limit, which can't be expressed
                as facet rows. Matches the ProxyLogsViewer layout. */}
            {showFilters && (
                <div className="px-6 py-3 border-b bg-surface-alt">
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3 items-end">
                        <div>
                            <label className="block text-xs font-medium text-secondary mb-1">Time Range</label>
                            <TimeRangePicker value={timeRange} onChange={setTimeRange} />
                        </div>
                        <div>
                            <label className="block text-xs font-medium text-secondary mb-1">Limit</label>
                            <select
                                value={limit}
                                onChange={(e) => setLimit(Number(e.target.value))}
                                className="w-full px-2 py-1 text-xs border border-strong rounded bg-surface"
                            >
                                <option value={50}>50 logs</option>
                                <option value={100}>100 logs</option>
                                <option value={250}>250 logs</option>
                                <option value={500}>500 logs</option>
                                <option value={1000}>1000 logs</option>
                            </select>
                        </div>
                    </div>
                </div>
            )}

            {/* Error Display */}
            {error && (
                <div className="mx-6 mt-4 bg-red-50 border border-red-200 rounded-lg p-3 flex items-center space-x-2">
                    <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0" />
                    <span className="text-sm text-red-700">{error}</span>
                    <button onClick={() => setError(null)} className="ml-auto text-red-600 hover:text-red-800">
                        <X className="w-4 h-4" />
                    </button>
                </div>
            )}

            {/* Table */}
            <div className="flex-1 overflow-auto min-h-0 min-w-0">
                {loading && logs.length === 0 ? (
                    <div className="flex items-center justify-center h-full">
                        <div className="text-center">
                            <RefreshCw className="w-12 h-12 mx-auto mb-4 text-muted animate-spin" />
                            <p className="text-muted">Loading audit logs...</p>
                        </div>
                    </div>
                ) : logs.length === 0 ? (
                    <div className="flex items-center justify-center h-full">
                        <div className="text-center text-muted">
                            <AlertCircle className="w-12 h-12 mx-auto mb-4 opacity-50" />
                            <p>No audit logs found</p>
                            <p className="text-sm mt-1">Try adjusting your filters</p>
                        </div>
                    </div>
                ) : (
                    <table className="w-full">
                        <thead className={`${TABLE_THEAD} sticky top-0 z-10`}>
                        <tr>
                            <th className={`${TABLE_TH} w-8`}></th>
                            <th className={`${TABLE_TH} w-12`}>Status</th>
                            <th className={TABLE_TH}>Timestamp</th>
                            <th className={TABLE_TH}>Action</th>
                            <th className={TABLE_TH}>User</th>
                            <th className={TABLE_TH}>Resource</th>
                            <th className={TABLE_TH}>IP Address</th>
                            <th className={TABLE_TH}>Method</th>
                        </tr>
                        </thead>
                        <tbody className="bg-surface divide-y divide-default">
                        {logs.map((log) => (
                            <React.Fragment key={log.id}>
                                <tr
                                    className="hover:bg-surface-alt cursor-pointer transition-colors"
                                    onClick={() => toggleRowExpansion(log.id)}
                                >
                                    <td className="px-4 py-3 whitespace-nowrap text-sm">
                                        {expandedRow === log.id ? (
                                            <ChevronDown className="w-4 h-4 text-muted" />
                                        ) : (
                                            <ChevronRight className="w-4 h-4 text-muted" />
                                        )}
                                    </td>
                                    <td className="px-4 py-3 whitespace-nowrap text-sm">
                                        {getStatusIcon(log.success)}
                                    </td>
                                    <td className="px-4 py-3 whitespace-nowrap text-sm text-default font-mono">
                                        {formatTimestamp(log.timestamp)}
                                    </td>
                                    <td className="px-4 py-3 whitespace-nowrap text-sm">
                      <span className={`px-2 py-1 rounded-md text-xs font-medium ${getActionBadgeColor(log.action)}`}>
                        {log.action || <span className="text-muted">Unknown</span>}
                      </span>
                                    </td>
                                    <td className="px-4 py-3 whitespace-nowrap text-sm text-secondary font-mono">
                                        {log.admin_id || <span className="text-muted">—</span>}
                                    </td>
                                    <td className="px-4 py-3 text-sm text-secondary">
                                        {log.resource_type && log.resource_id ? (
                                            <div className="flex flex-col">
                                                <span className="text-xs text-muted">{log.resource_type}</span>
                                                <span className="font-mono text-xs">{log.resource_id}</span>
                                            </div>
                                        ) : (
                                            <span className="text-muted">—</span>
                                        )}
                                    </td>
                                    <td className="px-4 py-3 whitespace-nowrap text-sm text-secondary font-mono">
                                        {log.ip_address || <span className="text-muted">—</span>}
                                    </td>
                                    <td className="px-4 py-3 whitespace-nowrap text-sm">
                                        {log.request_method ? (
                                            <span className={`px-2 py-1 rounded text-xs font-medium ${
                                                log.request_method === 'GET' ? 'bg-blue-100 text-blue-800' :
                                                    log.request_method === 'POST' ? 'bg-green-100 text-green-800' :
                                                        log.request_method === 'PUT' || log.request_method === 'PATCH' ? 'bg-yellow-100 text-yellow-800' :
                                                            log.request_method === 'DELETE' ? 'bg-red-100 text-red-800' :
                                                                'bg-surface-raised text-default'
                                            }`}>
                          {log.request_method}
                        </span>
                                        ) : (
                                            <span className="text-muted">—</span>
                                        )}
                                    </td>
                                </tr>

                                {/* Expanded Row Details */}
                                {expandedRow === log.id && (
                                    <tr className="bg-surface-alt">
                                        <td colSpan={8} className="px-4 py-4">
                                            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                                {/* Request Details */}
                                                <div>
                                                    <h4 className="text-sm font-semibold text-default mb-2">Request Details</h4>
                                                    <dl className="space-y-1">
                                                        {log.request_path && (
                                                            <div className="flex items-start">
                                                                <dt className="text-xs font-medium text-muted w-24">Path:</dt>
                                                                <dd className="text-xs text-default font-mono flex-1 min-h-0 min-w-0">{log.request_path}</dd>
                                                            </div>
                                                        )}
                                                        {log.user_agent && (
                                                            <div className="flex items-start">
                                                                <dt className="text-xs font-medium text-muted w-24">User Agent:</dt>
                                                                <dd className="text-xs text-secondary flex-1 break-words min-h-0 min-w-0">{log.user_agent}</dd>
                                                            </div>
                                                        )}
                                                        {log.id && (
                                                            <div className="flex items-start">
                                                                <dt className="text-xs font-medium text-muted w-24">Log ID:</dt>
                                                                <dd className="text-xs text-secondary font-mono flex-1 min-h-0 min-w-0">{log.id}</dd>
                                                            </div>
                                                        )}
                                                    </dl>
                                                </div>

                                                {/* Additional Details */}
                                                {log.details && Object.keys(log.details).length > 0 && (
                                                    <div>
                                                        <h4 className="text-sm font-semibold text-default mb-2">Additional Details</h4>
                                                        <div className="bg-surface rounded border p-2 max-h-40 overflow-auto">
                                <pre className="text-xs text-secondary whitespace-pre-wrap font-mono">
                                  {JSON.stringify(log.details, null, 2)}
                                </pre>
                                                        </div>
                                                    </div>
                                                )}
                                            </div>
                                        </td>
                                    </tr>
                                )}
                            </React.Fragment>
                        ))}
                        </tbody>
                    </table>
                )}
            </div>

            {/* Footer with Stats */}
            <div className="px-6 py-3 border-t bg-surface-alt flex items-center justify-between text-xs text-muted">
                <div className="flex items-center space-x-4">
                    <span>Total: {logs.length} logs</span>
                    <span>
            Success: {successCount} | Failed: {logs.length - successCount}
          </span>
                </div>
                {isLive && (
                    <span className="text-green-600 font-medium flex items-center space-x-1">
            <div className="w-2 h-2 rounded-full bg-green-600 animate-pulse"></div>
            <span>Auto-refreshing every 10s</span>
          </span>
                )}
            </div>
                </div>{/* /right column */}
            </div>{/* /horizontal flex row containing sidebar + right column */}
        </div>
    );
};

export default AuditLogViewer;
/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Trace timeline view.
 *
 * Renders every span of a single trace, ordered by
 * (delegation_depth, timestamp), with HMAC-verification status
 * icons per step and inline protocol + status badges.
 *
 * Triggered from the Proxy Logs table's "View Trace" action on
 * any row carrying a `root_task_id` — for spans without a
 * trace the action is hidden.
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
    AlertTriangle,
    CheckCircle2,
    Loader2,
    ShieldAlert,
    ShieldCheck,
    ShieldOff,
    Sparkles,
    X,
    XCircle,
} from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import { runBackground } from '../../lib/asyncSafe';


interface TraceSpan {
    id: string;
    timestamp: string | null;
    proxy_type: string;
    proxy_subtype: string | null;
    request_id: string | null;
    parent_task_id: string | null;
    current_task_id: string | null;
    delegation_depth: number | null;
    trace_continuity: string | null;
    trace_root_kind: string | null;
    endpoint: string | null;
    http_method: string | null;
    http_status_code: number | null;
    duration_ms: number | null;
    success: boolean | null;
    error_message: string | null;
    gateway_user_id: string | null;
    idp_user_email: string | null;
    server_id: string | null;
    mcp_capability_name: string | null;
    llm_model_used: string | null;
    llm_provider: string | null;
}


interface TraceResponse {
    root_task_id: string;
    tenant_id: string;
    total_spans: number;
    max_depth: number;
    proxy_types: string[];
    spans: TraceSpan[];
}


function continuityIcon(c: string | null) {
    // Mirror of the audit dashboard's HMAC-verification status
    // icon — operators learn one vocabulary across the
    // proxy-logs view and the per-trace timeline.
    switch (c) {
        case 'verified':
            return { Icon: ShieldCheck, color: 'text-emerald-600', label: 'Signature verified' };
        case 'minted':
            return { Icon: Sparkles, color: 'text-blue-600', label: 'Root chain minted' };
        case 'client_dropped':
            return { Icon: AlertTriangle, color: 'text-amber-600', label: 'Client dropped headers — fragmented trace' };
        case 'sig_failed':
            return { Icon: ShieldOff, color: 'text-red-600', label: 'Signature verification failed' };
        default:
            return { Icon: ShieldAlert, color: 'text-gray-400', label: 'Continuity not stamped' };
    }
}


function protocolBadgeColor(proxyType: string): string {
    switch (proxyType) {
        case 'llm': return 'bg-purple-100 text-purple-800 dark:bg-purple-900/40 dark:text-purple-300';
        case 'mcp': return 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300';
        case 'a2a': return 'bg-orange-100 text-orange-800 dark:bg-orange-900/40 dark:text-orange-300';
        case 'skill': return 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300';
        case 'oauth': return 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300';
        default: return 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-400';
    }
}


interface Props {
    rootTaskId: string;
    onClose: () => void;
}


const TraceTimelineModal: React.FC<Props> = ({ rootTaskId, onClose }) => {
    const { gatewayClient } = useGateway();
    const [trace, setTrace] = useState<TraceResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string>('');

    const fetchTrace = useCallback(async () => {
        setLoading(true);
        setError('');
        try {
            const data = await gatewayClient.getProxyLogTrace(rootTaskId);
            setTrace(data);
        } catch (e: unknown) {
            const msg = e instanceof Error ? e.message : String(e);
            setError(msg);
        } finally {
            setLoading(false);
        }
    }, [gatewayClient, rootTaskId]);

    useEffect(() => { runBackground(fetchTrace(), 'trace'); }, [fetchTrace]);

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
            <div className="w-full max-w-5xl max-h-[90vh] overflow-hidden rounded-lg bg-white shadow-xl dark:bg-gray-900">
                {/* Header */}
                <div className="flex items-start justify-between border-b border-gray-200 bg-white p-5 dark:border-gray-700 dark:bg-gray-900">
                    <div className="flex-1 min-w-0 min-h-0">
                        <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
                            Trace timeline
                        </h2>
                        <p className="mt-1 truncate font-mono text-xs text-gray-500 dark:text-gray-400">
                            root_task_id: {rootTaskId}
                        </p>
                        {trace && (
                            <div className="mt-2 flex flex-wrap gap-2 text-xs">
                                <span className="rounded bg-gray-100 px-2 py-0.5 text-gray-700 dark:bg-gray-800 dark:text-gray-300">
                                    {trace.total_spans} span{trace.total_spans === 1 ? '' : 's'}
                                </span>
                                <span className="rounded bg-gray-100 px-2 py-0.5 text-gray-700 dark:bg-gray-800 dark:text-gray-300">
                                    depth {trace.max_depth}
                                </span>
                                {trace.proxy_types.map((p) => (
                                    <span key={p} className={`rounded px-2 py-0.5 ${protocolBadgeColor(p)}`}>
                                        {p}
                                    </span>
                                ))}
                            </div>
                        )}
                    </div>
                    <button
                        onClick={onClose}
                        aria-label="Close"
                        className="rounded-md p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800"
                    >
                        <X className="h-5 w-5" />
                    </button>
                </div>

                {/* Body */}
                <div className="overflow-y-auto max-h-[calc(90vh-110px)] p-5">
                    {loading && (
                        <div className="flex items-center justify-center py-12">
                            <Loader2 className="h-6 w-6 animate-spin text-gray-400" />
                        </div>
                    )}
                    {error && !loading && (
                        <div className="rounded-md border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
                            Failed to load trace: {error}
                        </div>
                    )}
                    {trace && !loading && (
                        <ol className="relative space-y-3">
                            {trace.spans.map((span, idx) => {
                                const depth = span.delegation_depth ?? 0;
                                const cont = continuityIcon(span.trace_continuity);
                                const { Icon: ContIcon, color, label } = cont;
                                return (
                                    <li
                                        key={span.id}
                                        className="rounded-md border border-gray-200 bg-white p-3 shadow-sm dark:border-gray-700 dark:bg-gray-800"
                                        style={{ marginLeft: `${depth * 24}px` }}
                                    >
                                        <div className="flex items-start gap-3">
                                            <span title={label} className="inline-flex">
                                                <ContIcon className={`mt-0.5 h-5 w-5 flex-shrink-0 ${color}`} />
                                            </span>
                                            <div className="min-w-0 flex-1 min-h-0">
                                                <div className="flex flex-wrap items-center gap-2">
                                                    <span className="text-xs font-medium text-gray-500 dark:text-gray-400">
                                                        #{idx + 1}
                                                    </span>
                                                    <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${protocolBadgeColor(span.proxy_type)}`}>
                                                        {span.proxy_type}
                                                    </span>
                                                    {span.proxy_subtype && (
                                                        <span className="text-xs text-gray-500 dark:text-gray-400">
                                                            {span.proxy_subtype}
                                                        </span>
                                                    )}
                                                    {span.success === true && (
                                                        <CheckCircle2 className="h-4 w-4 text-emerald-500" aria-label="success" />
                                                    )}
                                                    {span.success === false && (
                                                        <XCircle className="h-4 w-4 text-red-500" aria-label="failure" />
                                                    )}
                                                    {span.http_status_code != null && (
                                                        <span className="text-xs text-gray-600 dark:text-gray-400">
                                                            HTTP {span.http_status_code}
                                                        </span>
                                                    )}
                                                    {span.duration_ms != null && (
                                                        <span className="text-xs text-gray-500 dark:text-gray-400">
                                                            {Math.round(span.duration_ms)}ms
                                                        </span>
                                                    )}
                                                </div>
                                                {span.endpoint && (
                                                    <div className="mt-1 truncate font-mono text-xs text-gray-700 dark:text-gray-300">
                                                        {span.http_method ?? ''} {span.endpoint}
                                                    </div>
                                                )}
                                                <div className="mt-1 flex flex-wrap gap-3 text-xs text-gray-500 dark:text-gray-400">
                                                    {span.mcp_capability_name && (
                                                        <span>capability: <code>{span.mcp_capability_name}</code></span>
                                                    )}
                                                    {span.llm_provider && (
                                                        <span>provider: {span.llm_provider}</span>
                                                    )}
                                                    {span.llm_model_used && (
                                                        <span>model: {span.llm_model_used}</span>
                                                    )}
                                                    {span.server_id && (
                                                        <span>server: {span.server_id}</span>
                                                    )}
                                                    {span.gateway_user_id && (
                                                        <span>user: {span.gateway_user_id}</span>
                                                    )}
                                                    {span.timestamp && (
                                                        <span>{new Date(span.timestamp).toLocaleString()}</span>
                                                    )}
                                                </div>
                                                {span.error_message && (
                                                    <div className="mt-1 text-xs text-red-600 dark:text-red-400">
                                                        {span.error_message}
                                                    </div>
                                                )}
                                            </div>
                                        </div>
                                    </li>
                                );
                            })}
                        </ol>
                    )}
                </div>
            </div>
        </div>
    );
};

export default TraceTimelineModal;

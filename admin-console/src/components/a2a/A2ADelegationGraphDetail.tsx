/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Drill-down view for one A2A delegation graph.
 *
 * Steps arrive from the server ordered by (depth, timestamp). We
 * render them as an indented tree — each step at depth N is grouped
 * under the step whose `current_task_id` matches its `parent_task_id`.
 */

import React, { useEffect, useState } from 'react';
import {
    ArrowDownLeft,
    ArrowUpRight,
    ChevronLeft,
    GitBranch,
    Info,
    Loader2,
    RefreshCw,
} from 'lucide-react';

import { useAuth } from '../../hooks/useAuth';
import InfoBanner from '../common/InfoBanner';
import type { A2ADelegationGraphDetailResponse } from '../../lib/a2aTypes';
import {
    directionBadge,
    formatDate,
    formatDurationMs,
} from './a2aFormatters';
import { buildTree, type TreeNode } from './delegationGraphTree';
import { errorMessage } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';

interface Props {
    rootTaskId: string;
    onBack: () => void;
}

const A2ADelegationGraphDetail: React.FC<Props> = ({ rootTaskId, onBack }) => {
    const { gatewayClient } = useAuth();
    const [graph, setGraph] = useState<A2ADelegationGraphDetailResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const load = async () => {
        setError(null);
        setLoading(true);
        try {
            const res = await gatewayClient.getA2ADelegationGraph(rootTaskId);
            setGraph(res);
        } catch (e) {
            setError(errorMessage(e) ?? 'Failed to load delegation graph');
        } finally {
            setLoading(false);
        }
    };

    async function reload() {
        setRefreshing(true);
        try {
            const res = await gatewayClient.getA2ADelegationGraph(rootTaskId);
            setGraph(res);
            setError(null);
        } catch (e) {
            setError(errorMessage(e) ?? 'Failed to load delegation graph');
        } finally {
            setRefreshing(false);
        }
    }

    useEffect(() => {
        runBackground(load(), 'load');
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [rootTaskId]);

    const tree = graph ? buildTree(graph.steps) : [];

    return (
        <div className="p-6 space-y-6">
            {/* ============ HEADER ============ */}
            <div className="flex items-center justify-between">
                <div className="flex items-center space-x-3">
                    <button
                        onClick={onBack}
                        className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors"
                        title="Back to list"
                    >
                        <ChevronLeft className="w-5 h-5" />
                    </button>
                    <div className="p-2.5 rounded-xl bg-primary-100">
                        <GitBranch className="w-7 h-7 text-primary-600" />
                    </div>
                    <div className="min-w-0">
                        <h1 className="text-2xl font-bold text-default">
                            Delegation Graph
                        </h1>
                        <p
                            className="text-xs text-muted font-mono truncate"
                            title={rootTaskId}
                        >
                            root {rootTaskId}
                        </p>
                    </div>
                </div>

                <button
                    onClick={asyncHandler(reload, 'click')}
                    disabled={refreshing || loading}
                    className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors disabled:opacity-50"
                    title="Refresh"
                >
                    <RefreshCw
                        className={`w-5 h-5 ${refreshing ? 'animate-spin' : ''}`}
                    />
                </button>
            </div>

            {/* ============ INFO BOX ============ */}
            <div className="rounded-lg bg-primary-50 border border-primary-200 p-4">
                <div className="flex items-start space-x-3">
                    <Info className="w-5 h-5 text-primary-500 mt-0.5 flex-shrink-0" />
                    <div className="text-sm text-primary-700">
                        <p className="font-medium mb-1">Reading the step tree</p>
                        <p className="text-xs text-primary-600">
                            Each row below is one step of this delegation — the gateway
                            entering an <strong>inbound</strong> call or issuing an{' '}
                            <strong>outbound</strong> one. Rows are indented by depth so the
                            parent/child structure reads top-to-bottom. A step’s{' '}
                            <strong>current_task_id</strong> is its node id; its{' '}
                            <strong>parent_task_id</strong> points to the step that invoked it,
                            or is null at the root. A badge marked <em>detached</em> means the
                            step’s parent wasn’t found in this graph — typically a
                            log-retention cutoff rather than a real gap. Status code, latency,
                            and error message on the right summarise the outcome.
                        </p>
                    </div>
                </div>
            </div>

            {error && (
                <InfoBanner variant="error" className="mb-0">
                    <p>{error}</p>
                </InfoBanner>
            )}

            {/* ============ GRAPH CARD ============ */}
            {loading && !graph ? (
                <div className="flex items-center justify-center py-16">
                    <Loader2 className="w-8 h-8 text-primary-600 animate-spin" />
                </div>
            ) : graph ? (
                <div className="bg-surface border border-default rounded-lg shadow-sm">
                    <div className="border-b border-default px-5 py-4 flex flex-wrap items-center justify-between gap-4">
                        <div className="flex items-center space-x-6 text-sm">
                            <Stat label="Steps" value={String(graph.total_steps)} />
                            <Stat label="Max depth" value={String(graph.max_depth)} />
                            <Stat label="Tenant" value={graph.tenant_id} mono />
                        </div>
                    </div>

                    {graph.steps.length === 0 ? (
                        <div className="py-12 text-center text-secondary text-sm">
                            No steps
                        </div>
                    ) : (
                        <ol className="p-4 space-y-2">
                            {tree.map(node => (
                                <StepRow key={node.step.proxy_log_id} node={node} />
                            ))}
                        </ol>
                    )}
                </div>
            ) : null}
        </div>
    );
};

export default A2ADelegationGraphDetail;

// ────────────────────────────────────────────────────────────────────────────
// Row renderer — tree construction lives in `./delegationGraphTree.ts`
// so the pure logic is independently unit-testable.
// ────────────────────────────────────────────────────────────────────────────

const StepRow: React.FC<{ node: TreeNode }> = ({ node }) => {
    const dir = directionBadge(node.step.direction);
    const DirIcon = node.step.direction === 'inbound' ? ArrowDownLeft : ArrowUpRight;
    return (
        <li
            className="grid grid-cols-[auto,1fr,auto] items-center gap-4 rounded-lg border border-default bg-surface-alt px-3 py-2"
            style={{ marginLeft: `${Math.min(node.indent, 12) * 16}px` }}
        >
            <div className="flex items-center space-x-2">
                <span
                    className={`flex items-center space-x-1 px-2 py-0.5 text-xs font-medium rounded ${dir.bg} ${dir.text}`}
                >
                    <DirIcon className="w-3 h-3" />
                    <span>{dir.label}</span>
                </span>
                <span className="px-1.5 py-0.5 text-xs rounded bg-surface-raised text-muted font-mono">
                    d{node.step.depth}
                </span>
                {node.detached && (
                    <span
                        className="px-1.5 py-0.5 text-xs rounded bg-amber-100 text-amber-700"
                        title="Parent step was not found in this graph"
                    >
                        detached
                    </span>
                )}
            </div>

            <div className="min-w-0">
                <div className="flex items-center gap-1.5 text-sm font-medium text-default">
                    <span className="truncate" title={node.step.actor_agent_id ?? node.step.subject_id ?? ''}>
                        {node.step.actor_agent_name ?? node.step.subject_id ?? 'caller'}
                    </span>
                    <span className="text-muted">{'→'}</span>
                    <span className="truncate text-primary-600" title={node.step.card_id ?? ''}>
                        {node.step.card_name ?? node.step.card_id ?? 'callee'}
                    </span>
                </div>
                <div className="flex items-center space-x-2 text-sm">
                    <code
                        className="truncate font-mono text-xs text-muted"
                        title={node.step.current_task_id ?? ''}
                    >
                        {node.step.current_task_id ?? '—'}
                    </code>
                    {node.step.parent_task_id && (
                        <span className="text-xs text-muted">
                            ←{' '}
                            <code className="font-mono">
                                {node.step.parent_task_id.slice(0, 10)}…
                            </code>
                        </span>
                    )}
                </div>
                <div className="mt-0.5 truncate text-xs text-muted">
                    {node.step.endpoint}
                    {node.step.target_url ? ` → ${node.step.target_url}` : ''}
                </div>
                {node.step.error_message && (
                    <div className="mt-1 text-xs text-red-600">
                        {node.step.error_message}
                    </div>
                )}
            </div>

            <div className="flex flex-col items-end gap-0.5 text-xs text-muted whitespace-nowrap">
                <span
                    className={
                        node.step.success ? 'text-green-600' : 'text-red-600'
                    }
                >
                    {node.step.http_status_code ?? '—'}
                </span>
                <span>{formatDurationMs(node.step.duration_ms)}</span>
                <span>{formatDate(node.step.timestamp)}</span>
            </div>
        </li>
    );
};

const Stat: React.FC<{ label: string; value: string; mono?: boolean }> = ({
    label,
    value,
    mono,
}) => (
    <div>
        <div className="text-xs uppercase tracking-wide text-muted">{label}</div>
        <div className={`text-sm text-default ${mono ? 'font-mono' : ''}`}>
            {value}
        </div>
    </div>
);

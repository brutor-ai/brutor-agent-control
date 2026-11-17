/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Read-only browser for A2A v1.0 stateful tasks.
 *
 * Rows are authored by the Rust proxy on every inbound SendMessage /
 * SendStreamingMessage hit and mutated through the task lifecycle
 * (SUBMITTED → WORKING → COMPLETED / FAILED / CANCELED / ...). This
 * admin tab is purely observational — it doesn't let admins mutate
 * tasks because the proxy owns the lifecycle.
 *
 * The detail view is a left-navbar modal matching the rest of the
 * A2A surface, with tabs mirroring the shape of the Task
 * JSON: Status, History, Artifacts, Push Configs, Metadata, Raw JSON.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
    Archive,
    Clock,
    FileCode2,
    History,
    Info,
    ListChecks,
    Loader2,
    Mail,
    RefreshCw,
    Settings,
    X,
} from 'lucide-react';

import { useAuth } from '../../hooks/useAuth';
import InfoBanner from '../common/InfoBanner';
import { useToast } from '../common/Toast';
import type {
    A2APushConfigListItem,
    A2ATaskDetailResponse,
    A2ATaskListItem,
} from '../../lib/a2aTypes';
import { formatDate } from './a2aFormatters';
import { errorMessage } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';

const TASK_STATES: string[] = [
    'TASK_STATE_SUBMITTED',
    'TASK_STATE_WORKING',
    'TASK_STATE_INPUT_REQUIRED',
    'TASK_STATE_AUTH_REQUIRED',
    'TASK_STATE_COMPLETED',
    'TASK_STATE_FAILED',
    'TASK_STATE_CANCELED',
    'TASK_STATE_REJECTED',
];

const A2ATasksTab: React.FC = () => {
    const { gatewayClient } = useAuth();
    const toast = useToast();

    const [tasks, setTasks] = useState<A2ATaskListItem[]>([]);
    const [total, setTotal] = useState(0);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // Filters
    const [stateFilter, setStateFilter] = useState<string>('');
    // What is in the box vs what the list is filtered by. The box says "press Enter
    // to apply", and it is an exact match, so a request per keystroke returned
    // nothing and flashed the spinner over the list while the operator typed.
    const [contextId, setContextId] = useState('');
    const [appliedContextId, setAppliedContextId] = useState('');
    const [limit] = useState(50);
    const [offset, setOffset] = useState(0);

    const [activeTask, setActiveTask] = useState<A2ATaskDetailResponse | null>(null);
    const [detailLoading, setDetailLoading] = useState(false);

    const load = useCallback(async () => {
        setError(null);
        try {
            const resp = await gatewayClient.listA2ATasks({
                state: stateFilter || undefined,
                context_id: appliedContextId || undefined,
                limit,
                offset,
            });
            setTasks(resp.tasks);
            setTotal(resp.total);
        } catch (e) {
            setError(errorMessage(e) ?? 'Failed to load tasks');
        }
    }, [gatewayClient, stateFilter, appliedContextId, limit, offset]);

    useEffect(() => {
        setLoading(true);
        runBackground(load().finally(() => setLoading(false)), 'load');
    }, [load]);

    async function reload() {
        setRefreshing(true);
        try {
            await load();
        } finally {
            setRefreshing(false);
        }
    }

    async function openDetail(t: A2ATaskListItem) {
        setDetailLoading(true);
        try {
            const detail = await gatewayClient.getA2ATask(t.id);
            setActiveTask(detail);
        } catch (e) {
            toast.error('Load failed', errorMessage(e) ?? 'Failed to load task');
        } finally {
            setDetailLoading(false);
        }
    }

    const canPrev = offset > 0;
    const canNext = offset + limit < total;

    return (
        <div className="p-6 space-y-6">
            {/* ============ HEADER ============ */}
            <div className="flex items-center justify-between">
                <div className="flex items-center space-x-3">
                    <div
                        className={`p-2.5 rounded-xl ${
                            tasks.length > 0 ? 'bg-primary-100' : 'bg-surface-raised'
                        }`}
                    >
                        <ListChecks
                            className={`w-7 h-7 ${
                                tasks.length > 0 ? 'text-primary-600' : 'text-muted'
                            }`}
                        />
                    </div>
                    <div>
                        <h1 className="text-2xl font-bold text-default">A2A Tasks</h1>
                        <p className="text-sm text-muted">
                            {total === 0
                                ? 'Stateful A2A v1.0 tasks authored by the proxy'
                                : `${total} task${total === 1 ? '' : 's'} matching filters`}
                        </p>
                    </div>
                </div>

                <div className="flex items-center space-x-3">
                    <button
                        onClick={asyncHandler(reload, 'click')}
                        disabled={refreshing}
                        className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors disabled:opacity-50"
                        title="Refresh"
                    >
                        <RefreshCw
                            className={`w-5 h-5 ${refreshing ? 'animate-spin' : ''}`}
                        />
                    </button>
                </div>
            </div>

            {/* ============ INFO ============ */}
            <InfoBanner title="Read-only task registry">
                <p>
                    Tasks are created by the gateway on every inbound v1.0
                    invocation and mutated as the work progresses. Admins can
                    browse state, history, artifacts, and push-notification
                    configs — changes happen through the A2A protocol, not from this console.
                </p>
            </InfoBanner>

            {/* ============ FILTERS ============ */}
            <div className="bg-surface border border-default rounded-lg p-4 flex flex-wrap items-end gap-3">
                <div className="flex-1 min-w-[160px] min-h-0 min-w-0">
                    <label className="block text-xs text-muted mb-1">State</label>
                    <select
                        value={stateFilter}
                        onChange={e => {
                            setStateFilter(e.target.value);
                            setOffset(0);
                        }}
                        className="w-full px-3 py-2 border border-strong rounded-md bg-surface text-sm"
                    >
                        <option value="">Any</option>
                        {TASK_STATES.map(s => (
                            <option key={s} value={s}>
                                {s}
                            </option>
                        ))}
                    </select>
                </div>
                <div className="flex-1 min-w-[200px] min-h-0 min-w-0">
                    <label className="block text-xs text-muted mb-1">Context ID</label>
                    <input
                        type="text"
                        value={contextId}
                        onChange={e => setContextId(e.target.value)}
                        onKeyDown={e => {
                            if (e.key === 'Enter') {
                                setOffset(0);
                                setAppliedContextId(contextId.trim());
                            }
                        }}
                        className="w-full px-3 py-2 border border-strong rounded-md bg-surface text-sm font-mono"
                        placeholder="Exact match — press Enter to apply"
                    />
                </div>
                {(stateFilter || contextId) && (
                    <button
                        onClick={() => {
                            setStateFilter('');
                            setContextId('');
                            setAppliedContextId('');
                            setOffset(0);
                        }}
                        className="px-3 py-2 text-sm text-secondary hover:text-default border border-strong rounded-lg"
                    >
                        Clear
                    </button>
                )}
            </div>

            {error && (
                <InfoBanner variant="error" className="mb-0">
                    <p>{error}</p>
                </InfoBanner>
            )}

            {/* ============ LIST ============ */}
            {loading ? (
                <div className="flex items-center justify-center py-16">
                    <Loader2 className="w-8 h-8 text-primary-600 animate-spin" />
                </div>
            ) : tasks.length === 0 ? (
                <div className="text-center py-16 bg-surface rounded-xl border-2 border-dashed border-default">
                    <ListChecks className="w-10 h-10 text-muted mx-auto mb-3" />
                    <h3 className="text-base font-semibold text-default mb-1">
                        No tasks found
                    </h3>
                    <p className="text-sm text-muted max-w-md mx-auto">
                        Tasks appear here after external callers hit the v1.0 message
                        endpoints on this tenant's agent cards.
                    </p>
                </div>
            ) : (
                <div className="bg-surface border border-default rounded-lg shadow-sm">
                    <ul className="divide-y divide-default">
                        {tasks.map(t => (
                            <li
                                key={t.id}
                                onClick={asyncHandler(() => openDetail(t), 'click')}
                                className="cursor-pointer px-5 py-3 hover:bg-surface-alt transition-colors"
                            >
                                <div className="flex items-start justify-between gap-4">
                                    <div className="min-w-0 flex-1 min-h-0">
                                        <div className="flex items-center gap-2">
                                            <StateBadge state={t.state} />
                                            <code className="text-xs text-muted truncate">
                                                {t.id}
                                            </code>
                                        </div>
                                        {t.context_id && (
                                            <p className="mt-1 text-xs text-muted font-mono truncate">
                                                ctx={t.context_id}
                                            </p>
                                        )}
                                    </div>
                                    <div className="flex flex-col items-end gap-0.5 text-xs text-muted whitespace-nowrap">
                                        <span>
                                            <Clock className="inline w-3 h-3 mr-1" />
                                            {formatDate(t.status_timestamp)}
                                        </span>
                                        {t.resource_group_id && (
                                            <span title="Resource group">
                                                rg={t.resource_group_id.slice(0, 8)}…
                                            </span>
                                        )}
                                    </div>
                                </div>
                            </li>
                        ))}
                    </ul>

                    <div className="flex items-center justify-between px-5 py-3 border-t border-default text-xs text-muted">
                        <span>
                            Showing {offset + 1}–{Math.min(offset + tasks.length, total)}{' '}
                            of {total}
                        </span>
                        <div className="flex items-center gap-2">
                            <button
                                onClick={() => setOffset(Math.max(0, offset - limit))}
                                disabled={!canPrev}
                                className="px-3 py-1 border border-strong rounded disabled:opacity-50"
                            >
                                Previous
                            </button>
                            <button
                                onClick={() => setOffset(offset + limit)}
                                disabled={!canNext}
                                className="px-3 py-1 border border-strong rounded disabled:opacity-50"
                            >
                                Next
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {(activeTask || detailLoading) && (
                <A2ATaskDetailDialog
                    task={activeTask}
                    loading={detailLoading}
                    onClose={() => {
                        setActiveTask(null);
                        setDetailLoading(false);
                    }}
                />
            )}
        </div>
    );
};

export default A2ATasksTab;

// ─── Detail dialog (left-nav) ───────────────────────────────────────────────

type DetailTabId =
    | 'status'
    | 'history'
    | 'artifacts'
    | 'push'
    | 'metadata'
    | 'raw';

const DETAIL_TABS: { id: DetailTabId; label: string; icon: React.ReactNode }[] = [
    { id: 'status', label: 'Status', icon: <Settings className="w-4 h-4" /> },
    { id: 'history', label: 'History', icon: <History className="w-4 h-4" /> },
    { id: 'artifacts', label: 'Artifacts', icon: <Archive className="w-4 h-4" /> },
    { id: 'push', label: 'Push Configs', icon: <Mail className="w-4 h-4" /> },
    { id: 'metadata', label: 'Metadata', icon: <Info className="w-4 h-4" /> },
    { id: 'raw', label: 'Raw JSON', icon: <FileCode2 className="w-4 h-4" /> },
];

const A2ATaskDetailDialog: React.FC<{
    task: A2ATaskDetailResponse | null;
    loading: boolean;
    onClose: () => void;
}> = ({ task, loading, onClose }) => {
    const { gatewayClient } = useAuth();
    const [activeTab, setActiveTab] = useState<DetailTabId>('status');
    const [pushConfigs, setPushConfigs] = useState<A2APushConfigListItem[] | null>(
        null,
    );
    const [pushLoading, setPushLoading] = useState(false);
    const [pushError, setPushError] = useState<string | null>(null);

    useEffect(() => {
        if (activeTab !== 'push' || !task || pushConfigs !== null) return;
        setPushLoading(true);
        gatewayClient
            .listA2ATaskPushConfigs(task.id)
            .then(resp => setPushConfigs(resp.configs))
            .catch(e => {
                // Without this a failed fetch read as "none registered".
                setPushError(errorMessage(e, 'Failed to load push configs'));
                setPushConfigs([]);
            })
            .finally(() => setPushLoading(false));
    }, [activeTab, task, pushConfigs, gatewayClient]);

    const history = useMemo(
        () =>
            task && Array.isArray((task.task_json as any)?.history)
                ? ((task.task_json as any).history as Array<Record<string, unknown>>)
                : [],
        [task],
    );
    const artifacts = useMemo(
        () =>
            task && Array.isArray((task.task_json as any)?.artifacts)
                ? ((task.task_json as any).artifacts as Array<Record<string, unknown>>)
                : [],
        [task],
    );
    const metadata = useMemo(
        () =>
            task && task.task_json && typeof (task.task_json as any)?.metadata === 'object'
                ? ((task.task_json as any).metadata as Record<string, unknown>)
                : {},
        [task],
    );
    const status = useMemo(
        () =>
            task && task.task_json && typeof (task.task_json as any)?.status === 'object'
                ? ((task.task_json as any).status as Record<string, unknown>)
                : {},
        [task],
    );

    return (
        <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
            <div className="bg-surface rounded-xl shadow-2xl w-full max-w-5xl max-h-[90vh] flex flex-col">
                <div className="flex items-center justify-between px-6 py-4 border-b border-default">
                    <div>
                        <h2 className="text-lg font-semibold text-default">
                            Task Detail
                        </h2>
                        {task && (
                            <p className="text-xs text-muted mt-0.5 font-mono">
                                {task.id}
                            </p>
                        )}
                    </div>
                    <button
                        onClick={onClose}
                        className="p-1 text-muted hover:text-secondary rounded"
                        aria-label="Close"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>
                {loading || !task ? (
                    <div className="flex-1 flex items-center justify-center py-16 min-h-0 min-w-0">
                        <Loader2 className="w-8 h-8 text-primary-600 animate-spin" />
                    </div>
                ) : (
                    <div className="flex flex-1 min-h-0 min-w-0">
                        <nav className="w-48 border-r border-default py-3 overflow-y-auto flex-shrink-0">
                            {DETAIL_TABS.map(tab => (
                                <button
                                    key={tab.id}
                                    onClick={() => setActiveTab(tab.id)}
                                    className={`w-full flex items-center gap-2 px-4 py-2 text-sm transition-colors ${
                                        activeTab === tab.id
                                            ? 'bg-primary-50 text-primary-700 font-medium border-r-2 border-primary-500'
                                            : 'text-secondary hover:bg-surface-alt hover:text-default'
                                    }`}
                                >
                                    {tab.icon}
                                    {tab.label}
                                </button>
                            ))}
                        </nav>
                        <div className="scroll-region p-6">
                            {activeTab === 'status' && (
                                <StatusPanel task={task} status={status} />
                            )}
                            {activeTab === 'history' && (
                                <HistoryPanel history={history} />
                            )}
                            {activeTab === 'artifacts' && (
                                <ArtifactsPanel artifacts={artifacts} />
                            )}
                            {activeTab === 'push' && (
                                <PushPanel
                                    loading={pushLoading}
                                    configs={pushConfigs}
                                    error={pushError}
                                />
                            )}
                            {activeTab === 'metadata' && (
                                <MetadataPanel metadata={metadata} />
                            )}
                            {activeTab === 'raw' && (
                                <pre className="p-3 bg-surface-alt border border-default rounded text-xs font-mono whitespace-pre-wrap">
                                    {JSON.stringify(task.task_json, null, 2)}
                                </pre>
                            )}
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
};

// ─── Detail panels ──────────────────────────────────────────────────────────

const StatusPanel: React.FC<{
    task: A2ATaskDetailResponse;
    status: Record<string, unknown>;
}> = ({ task, status }) => (
    <div className="space-y-3 text-sm">
        <div className="flex items-center gap-3">
            <StateBadge state={task.state} />
            <span className="text-xs text-muted">
                Last updated {formatDate(task.status_timestamp)}
            </span>
        </div>
        <KeyValue label="Task ID" value={<code>{task.id}</code>} />
        <KeyValue label="Tenant" value={<code>{task.tenant_id}</code>} />
        {task.context_id && (
            <KeyValue label="Context ID" value={<code>{task.context_id}</code>} />
        )}
        {task.resource_group_id && (
            <KeyValue
                label="Resource group"
                value={<code>{task.resource_group_id}</code>}
            />
        )}
        {typeof status.message === 'object' && status.message && (
            <div className="mt-4">
                <h4 className="text-xs font-semibold uppercase text-muted tracking-wider mb-1">
                    Status message
                </h4>
                <pre className="p-2 bg-surface-alt border border-default rounded text-xs font-mono whitespace-pre-wrap">
                    {JSON.stringify(status.message, null, 2)}
                </pre>
            </div>
        )}
    </div>
);

const HistoryPanel: React.FC<{ history: Array<Record<string, unknown>> }> = ({
    history,
}) => {
    if (history.length === 0) {
        return <p className="text-sm text-muted">No history entries.</p>;
    }
    return (
        <ol className="space-y-2">
            {history.map((msg, idx) => (
                <li
                    key={idx}
                    className="p-3 border border-default rounded-lg bg-surface-alt"
                >
                    <div className="flex items-center justify-between text-xs text-muted mb-1">
                        <span>
                            #{idx + 1} · role={String((msg as any).role ?? 'UNKNOWN')}
                        </span>
                        <span className="font-mono">
                            {String((msg as any).messageId ?? '')}
                        </span>
                    </div>
                    <pre className="text-xs font-mono whitespace-pre-wrap">
                        {JSON.stringify(msg, null, 2)}
                    </pre>
                </li>
            ))}
        </ol>
    );
};

const ArtifactsPanel: React.FC<{ artifacts: Array<Record<string, unknown>> }> = ({
    artifacts,
}) => {
    if (artifacts.length === 0) {
        return <p className="text-sm text-muted">No artifacts.</p>;
    }
    return (
        <ol className="space-y-2">
            {artifacts.map((art, idx) => (
                <li
                    key={idx}
                    className="p-3 border border-default rounded-lg bg-surface-alt"
                >
                    <div className="flex items-center justify-between text-xs text-muted mb-1">
                        <span>
                            #{idx + 1}{' '}
                            {(art as any).name
                                ? `· name=${String((art as any).name)}`
                                : ''}
                        </span>
                        <span className="font-mono">
                            {String((art as any).artifactId ?? '')}
                        </span>
                    </div>
                    <pre className="text-xs font-mono whitespace-pre-wrap">
                        {JSON.stringify(art, null, 2)}
                    </pre>
                </li>
            ))}
        </ol>
    );
};

const PushPanel: React.FC<{
    loading: boolean;
    configs: A2APushConfigListItem[] | null;
    error: string | null;
}> = ({ loading, configs, error }) => {
    if (loading) {
        return (
            <div className="flex items-center gap-2 text-sm text-muted">
                <Loader2 className="w-4 h-4 animate-spin" />
                Loading push configs…
            </div>
        );
    }
    if (error) {
        return (
            <InfoBanner variant="error" className="mb-0">
                <p>{error}</p>
            </InfoBanner>
        );
    }
    if (!configs || configs.length === 0) {
        return (
            <p className="text-sm text-muted">
                No push-notification configs registered for this task.
            </p>
        );
    }
    return (
        <ul className="space-y-2">
            {configs.map(cfg => (
                <li
                    key={cfg.id}
                    className="p-3 border border-default rounded-lg bg-surface-alt"
                >
                    <div className="flex items-center justify-between text-xs text-muted mb-1">
                        <code>{cfg.id}</code>
                        <span>{formatDate(cfg.created_at)}</span>
                    </div>
                    <pre className="text-xs font-mono whitespace-pre-wrap">
                        {JSON.stringify(cfg.config_json, null, 2)}
                    </pre>
                </li>
            ))}
        </ul>
    );
};

const MetadataPanel: React.FC<{ metadata: Record<string, unknown> }> = ({
    metadata,
}) => {
    if (Object.keys(metadata).length === 0) {
        return <p className="text-sm text-muted">No metadata.</p>;
    }
    return (
        <pre className="p-3 bg-surface-alt border border-default rounded text-xs font-mono whitespace-pre-wrap">
            {JSON.stringify(metadata, null, 2)}
        </pre>
    );
};

// ─── Shared sub-components ──────────────────────────────────────────────────

const StateBadge: React.FC<{ state: string }> = ({ state }) => {
    const colour = (() => {
        switch (state) {
            case 'TASK_STATE_COMPLETED':
                return 'bg-green-100 text-green-700';
            case 'TASK_STATE_WORKING':
            case 'TASK_STATE_SUBMITTED':
                return 'bg-blue-100 text-blue-700';
            case 'TASK_STATE_FAILED':
            case 'TASK_STATE_CANCELED':
            case 'TASK_STATE_REJECTED':
                return 'bg-red-100 text-red-700';
            case 'TASK_STATE_INPUT_REQUIRED':
            case 'TASK_STATE_AUTH_REQUIRED':
                return 'bg-amber-100 text-amber-700';
            default:
                return 'bg-gray-100 text-gray-700';
        }
    })();
    return (
        <span className={`px-2 py-0.5 text-xs font-medium rounded ${colour}`}>
            {state.replace(/^TASK_STATE_/, '').toLowerCase()}
        </span>
    );
};

const KeyValue: React.FC<{ label: string; value: React.ReactNode }> = ({
    label,
    value,
}) => (
    <div className="flex items-start gap-3">
        <dt className="text-xs text-muted min-w-[140px]">{label}</dt>
        <dd className="text-default">{value}</dd>
    </div>
);

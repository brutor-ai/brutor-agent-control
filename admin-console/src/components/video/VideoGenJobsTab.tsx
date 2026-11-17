/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useCallback, useRef } from 'react';
import {
    RefreshCw, ChevronLeft, ChevronRight, XCircle, Trash2, Film,
    AlertTriangle, ChevronDown, ChevronUp, Loader,
} from 'lucide-react';
import toast from 'react-hot-toast';

import { useGateway } from '../../hooks/useGateway';
import { TABLE_TH_TYPE, TABLE_THEAD, TABLE_TBODY } from '../ui/tableStyles';
import { useConfirm } from '../common/ConfirmationDialog';
import {
    VideoJobSummary, VideoJobDetail, VideoJobQueryRequest,
    VideoStatisticsResponse, VideoJobPaginationInfo,
    VIDEO_JOB_STATUSES, VIDEO_JOB_TERMINAL_STATUSES, VIDEO_STATUS_COLORS,
    VideoJobStatus,
} from '../../lib/videoGenJobTypes';
import { errorMessage } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

// Refresh cadence while in-flight rows exist. Matches BatchJobsTab so
// the two views feel consistent. The Rust proxy ticks the polling
// loop every 10s, so 30s here gives us 2-3 ticks of progress per
// refresh — plenty for an admin view.
const REFRESH_INTERVAL_MS = 30_000;

const VideoGenJobsTab: React.FC = () => {
    const { gatewayClient } = useGateway();
    const confirm = useConfirm();

    const [jobs, setJobs] = useState<VideoJobSummary[]>([]);
    const [pagination, setPagination] = useState<VideoJobPaginationInfo | null>(null);
    const [stats, setStats] = useState<VideoStatisticsResponse | null>(null);
    const [readFailure, reads] = useReadFailures<'stats'>();
    const [loading, setLoading] = useState(true);

    const [statusFilter, setStatusFilter] = useState<VideoJobStatus | ''>('');
    const [hoursAgo, setHoursAgo] = useState<number | ''>(24);
    const [page, setPage] = useState(1);
    const pageSize = 20;
    const [sortBy, setSortBy] = useState<VideoJobQueryRequest['sort_by']>('created_at');
    const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('desc');

    // Expanded detail row. Inline rather than a side panel keeps
    // this view simpler — operators usually just want to see the
    // prompt + full error + provider request id, all of which fit
    // under the row. We lazily fetch the detail (the list payload
    // truncates `error` to ~160 chars).
    const [expandedJobId, setExpandedJobId] = useState<string | null>(null);
    const [detailCache, setDetailCache] = useState<Record<string, VideoJobDetail>>({});

    const refreshTimer = useRef<ReturnType<typeof setInterval> | null>(null);

    const loadJobs = useCallback(async () => {
        if (!gatewayClient) return;
        try {
            setLoading(true);
            const query: VideoJobQueryRequest = {
                page,
                page_size: pageSize,
                sort_by: sortBy,
                sort_order: sortOrder,
            };
            if (statusFilter) query.status = [statusFilter];
            if (hoursAgo) query.hours_ago = hoursAgo;

            const resp = await gatewayClient.queryVideoGenJobs(query);
            setJobs(resp.jobs);
            setPagination(resp.pagination);
        } catch (err) {
            toast.error(`Failed to load video jobs: ${errorMessage(err) ?? 'unknown error'}`);
        } finally {
            setLoading(false);
        }
    }, [gatewayClient, page, statusFilter, hoursAgo, sortBy, sortOrder]);

    const loadStats = useCallback(async () => {
        if (!gatewayClient) return;
        try {
            const resp = await gatewayClient.getVideoGenStatistics(
                hoursAgo ? { hours_ago: hoursAgo } : undefined,
            );
            setStats(resp);
            reads.loaded('stats');
        } catch (err) {
            // The strip keeps what it showed, marked as stale; no toast on a refresh.
            reads.failed('stats', 'Failed to load the statistics', err);
        }
    }, [gatewayClient, hoursAgo, reads]);

    useEffect(() => {
        runBackground(loadJobs(), 'jobs');
        runBackground(loadStats(), 'stats');
    }, [loadJobs, loadStats]);

    // Auto-refresh while any row is in flight. The Rust proxy
    // updates `updated_at` every poll tick, so the table stays
    // alive even when the row never actually transitions status
    // (e.g. a fal model erroring on every poll).
    useEffect(() => {
        const hasActive = jobs.some(
            (j) => !VIDEO_JOB_TERMINAL_STATUSES.includes(j.status),
        );
        if (hasActive) {
            refreshTimer.current = setInterval(() => {
                runBackground(loadJobs(), 'jobs');
                runBackground(loadStats(), 'stats');
            }, REFRESH_INTERVAL_MS);
        }
        return () => {
            if (refreshTimer.current) clearInterval(refreshTimer.current);
        };
    }, [jobs, loadJobs, loadStats]);

    const handleCancel = async (jobId: string) => {
        if (!gatewayClient) return;
        const ok = await confirm({
            title: 'Cancel video generation job?',
            message: (
                <>
                    Cancelling marks the job as <code>cancelled</code> in the gateway and stops the
                    poll loop. The upstream provider (fal / Sora) may still finish the render and
                    bill for the clip — this is a client-side disconnect, not a provider-side cancel.
                </>
            ),
            confirmLabel: 'Cancel job',
            cancelLabel: 'Keep running',
            variant: 'warning',
        });
        if (!ok) return;
        try {
            await gatewayClient.cancelVideoGenJob(jobId);
            toast.success('Job cancelled');
            await loadJobs();
            await loadStats();
        } catch (err) {
            toast.error(`Cancel failed: ${errorMessage(err) ?? 'unknown error'}`);
        }
    };

    const handleDelete = async (jobId: string) => {
        if (!gatewayClient) return;
        const ok = await confirm({
            title: 'Delete video generation job?',
            message: 'Permanently removes the row. The generated video file (if any) is unaffected.',
            confirmLabel: 'Delete',
            cancelLabel: 'Keep',
            variant: 'danger',
        });
        if (!ok) return;
        try {
            await gatewayClient.deleteVideoGenJob(jobId);
            toast.success('Job deleted');
            await loadJobs();
            await loadStats();
        } catch (err) {
            toast.error(`Delete failed: ${errorMessage(err) ?? 'unknown error'}`);
        }
    };

    const handleExpand = async (jobId: string) => {
        if (expandedJobId === jobId) {
            setExpandedJobId(null);
            return;
        }
        setExpandedJobId(jobId);
        if (!detailCache[jobId] && gatewayClient) {
            try {
                const resp = await gatewayClient.getVideoGenJobDetail(jobId);
                setDetailCache((prev) => ({ ...prev, [jobId]: resp.job }));
            } catch (err) {
                toast.error(`Detail fetch failed: ${errorMessage(err) ?? 'unknown error'}`);
                // Collapse, or the row says "Loading detail…" forever.
                setExpandedJobId((current) => (current === jobId ? null : current));
            }
        }
    };

    const handleSort = (col: NonNullable<VideoJobQueryRequest['sort_by']>) => {
        if (sortBy === col) {
            setSortOrder((s) => (s === 'asc' ? 'desc' : 'asc'));
        } else {
            setSortBy(col);
            setSortOrder('desc');
        }
        setPage(1);
    };

    const fmtCost = (n?: number) =>
        typeof n === 'number' ? `$${n.toFixed(4)}` : '—';
    const fmtTime = (s?: string) => (s ? new Date(s).toLocaleString() : '—');

    return (
        <div className="p-6 h-full overflow-auto">
            <div className="mb-4 flex items-center justify-between">
                <div className="flex items-center gap-2">
                    <Film className="w-5 h-5 text-primary-600" />
                    <h2 className="text-xl font-semibold">Video Generation Jobs</h2>
                </div>
                <button
                    onClick={() => { runBackground(loadJobs(), 'jobs'); runBackground(loadStats(), 'stats'); }}
                    className="inline-flex items-center gap-1 px-3 py-1.5 text-sm border rounded hover:bg-surface-alt"
                >
                    <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
                    Refresh
                </button>
            </div>

            <ReadFailureNotice failure={readFailure('stats')} onRetry={() => runBackground(loadStats(), 'stats')} />
            {/* Stats strip — matches the BatchJobs visual rhythm. */}
            {stats && (
                <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-4">
                    <StatCard label="Total" value={stats.total_jobs.toString()} />
                    <StatCard
                        label="Succeeded"
                        value={stats.succeeded_count.toString()}
                        tone="green"
                    />
                    <StatCard
                        label="In flight"
                        value={stats.in_flight_count.toString()}
                        tone="blue"
                    />
                    <StatCard
                        label="Failed"
                        value={stats.failed_count.toString()}
                        tone="red"
                    />
                    <StatCard
                        label="Cost"
                        value={fmtCost(stats.total_cost_usd)}
                    />
                </div>
            )}

            {/* Filters */}
            <div className="mb-3 flex flex-wrap items-end gap-3">
                <div>
                    <label className="block text-xs text-secondary mb-1">Status</label>
                    <select
                        value={statusFilter}
                        onChange={(e) => { setStatusFilter(e.target.value as any); setPage(1); }}
                        className="px-3 py-1.5 border rounded text-sm bg-surface"
                    >
                        <option value="">All</option>
                        {VIDEO_JOB_STATUSES.map((s) => (
                            <option key={s} value={s}>{s}</option>
                        ))}
                    </select>
                </div>
                <div>
                    <label className="block text-xs text-secondary mb-1">Last (hours)</label>
                    <select
                        value={hoursAgo === '' ? '' : String(hoursAgo)}
                        onChange={(e) => {
                            const v = e.target.value;
                            setHoursAgo(v === '' ? '' : parseInt(v, 10));
                            setPage(1);
                        }}
                        className="px-3 py-1.5 border rounded text-sm bg-surface"
                    >
                        <option value="1">1h</option>
                        <option value="24">24h</option>
                        <option value="168">7d</option>
                        <option value="720">30d</option>
                        <option value="">All time</option>
                    </select>
                </div>
            </div>

            {/* Table */}
            <div className="border rounded overflow-hidden">
                <table className="w-full text-sm">
                    <thead className={TABLE_THEAD}>
                        <tr>
                            <th className="px-3 py-2 text-left w-8"></th>
                            <th className={`px-3 py-2 text-left ${TABLE_TH_TYPE}`}>Job ID</th>
                            <th className={`px-3 py-2 text-left ${TABLE_TH_TYPE}`}>Model</th>
                            <th className={`px-3 py-2 text-left ${TABLE_TH_TYPE}`}>Status</th>
                            <SortableTh active={sortBy === 'duration_seconds'} order={sortOrder} onClick={() => handleSort('duration_seconds')}>Dur</SortableTh>
                            <SortableTh active={sortBy === 'cost_usd'} order={sortOrder} onClick={() => handleSort('cost_usd')}>Cost</SortableTh>
                            <SortableTh active={sortBy === 'created_at'} order={sortOrder} onClick={() => handleSort('created_at')}>Created</SortableTh>
                            <th className={`px-3 py-2 text-right ${TABLE_TH_TYPE}`}>Actions</th>
                        </tr>
                    </thead>
                    <tbody className={TABLE_TBODY}>
                        {loading && jobs.length === 0 && (
                            <tr><td colSpan={8} className="px-3 py-8 text-center text-muted">
                                <Loader className="w-5 h-5 animate-spin inline mr-2" />Loading…
                            </td></tr>
                        )}
                        {!loading && jobs.length === 0 && (
                            <tr><td colSpan={8} className="px-3 py-8 text-center text-muted">
                                No video generation jobs in the selected range.
                            </td></tr>
                        )}
                        {jobs.map((job) => {
                            const isExpanded = expandedJobId === job.id;
                            const isTerminal = VIDEO_JOB_TERMINAL_STATUSES.includes(job.status);
                            const detail = detailCache[job.id];
                            return (
                                <React.Fragment key={job.id}>
                                    <tr className="hover:bg-surface-alt/40 cursor-pointer" onClick={asyncHandler(() => handleExpand(job.id), 'click')}>
                                        <td className="px-3 py-2">
                                            {isExpanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                                        </td>
                                        <td className="px-3 py-2 font-mono text-xs">{job.id}</td>
                                        <td className="px-3 py-2">{job.model_name ?? job.model_id ?? '—'}</td>
                                        <td className="px-3 py-2">
                                            <span className={`px-2 py-0.5 rounded text-xs font-medium ${VIDEO_STATUS_COLORS[job.status]}`}>
                                                {job.status}
                                            </span>
                                            {job.error_preview && (
                                                <AlertTriangle className="inline w-3 h-3 text-red-500 ml-1" aria-label={job.error_preview} />
                                            )}
                                        </td>
                                        <td className="px-3 py-2">{job.duration_seconds ? `${job.duration_seconds}s` : '—'}</td>
                                        <td className="px-3 py-2">{fmtCost(job.cost_usd)}</td>
                                        <td className="px-3 py-2 text-xs text-secondary">{fmtTime(job.created_at)}</td>
                                        <td className="px-3 py-2 text-right" onClick={(e) => e.stopPropagation()}>
                                            {!isTerminal && (
                                                <button
                                                    onClick={asyncHandler(() => handleCancel(job.id), 'click')}
                                                    className="text-amber-600 hover:bg-amber-50 px-2 py-1 rounded text-xs inline-flex items-center gap-1"
                                                    title="Cancel job"
                                                >
                                                    <XCircle className="w-3.5 h-3.5" />Cancel
                                                </button>
                                            )}
                                            {isTerminal && (
                                                <button
                                                    onClick={asyncHandler(() => handleDelete(job.id), 'click')}
                                                    className="text-red-600 hover:bg-red-50 px-2 py-1 rounded text-xs inline-flex items-center gap-1 ml-1"
                                                    title="Delete row"
                                                >
                                                    <Trash2 className="w-3.5 h-3.5" />Delete
                                                </button>
                                            )}
                                        </td>
                                    </tr>
                                    {isExpanded && (
                                        <tr className="bg-surface-alt/30">
                                            <td colSpan={8} className="px-6 py-3 text-xs space-y-2">
                                                {!detail && <div className="text-muted">Loading detail…</div>}
                                                {detail && (
                                                    <>
                                                        <div><span className="text-secondary">Prompt:</span> {detail.prompt}</div>
                                                        <div><span className="text-secondary">Resolution:</span> {detail.resolution ?? '—'} · <span className="text-secondary">Duration:</span> {detail.duration_seconds ? `${detail.duration_seconds}s` : '—'}</div>
                                                        <div><span className="text-secondary">Provider request id:</span> <span className="font-mono break-all">{detail.provider_request_id ?? '—'}</span></div>
                                                        <div><span className="text-secondary">Output file:</span> <span className="font-mono">{detail.output_file_id ?? '—'}</span></div>
                                                        <div><span className="text-secondary">Actor:</span> {detail.actor_id ?? '—'} · <span className="text-secondary">Updated:</span> {fmtTime(detail.updated_at)} · <span className="text-secondary">Completed:</span> {fmtTime(detail.completed_at)}</div>
                                                        {detail.error && (
                                                            <div className="bg-red-50 border border-red-200 rounded p-2 mt-2">
                                                                <div className="text-red-700 font-medium mb-1">Error</div>
                                                                <pre className="whitespace-pre-wrap break-all text-red-900">{detail.error}</pre>
                                                            </div>
                                                        )}
                                                    </>
                                                )}
                                            </td>
                                        </tr>
                                    )}
                                </React.Fragment>
                            );
                        })}
                    </tbody>
                </table>
            </div>

            {/* Pagination */}
            {pagination && pagination.total_pages > 1 && (
                <div className="mt-3 flex items-center justify-end gap-2 text-sm">
                    <button
                        onClick={() => setPage((p) => Math.max(1, p - 1))}
                        disabled={page <= 1}
                        className="px-2 py-1 border rounded disabled:opacity-50"
                    >
                        <ChevronLeft className="w-4 h-4" />
                    </button>
                    <span>
                        Page {pagination.page} / {pagination.total_pages} · {pagination.total} total
                    </span>
                    <button
                        onClick={() => setPage((p) => p + 1)}
                        disabled={page >= pagination.total_pages}
                        className="px-2 py-1 border rounded disabled:opacity-50"
                    >
                        <ChevronRight className="w-4 h-4" />
                    </button>
                </div>
            )}
        </div>
    );
};

const StatCard: React.FC<{ label: string; value: string; tone?: 'green' | 'red' | 'blue' }> = ({
    label, value, tone,
}) => (
    <div className={
        'border rounded px-3 py-2 ' +
        (tone === 'green' ? 'bg-green-50 border-green-200' :
         tone === 'red' ? 'bg-red-50 border-red-200' :
         tone === 'blue' ? 'bg-blue-50 border-blue-200' :
         'bg-surface-alt')
    }>
        <div className="text-xs text-secondary">{label}</div>
        <div className="text-lg font-semibold">{value}</div>
    </div>
);

const SortableTh: React.FC<{
    active: boolean;
    order: 'asc' | 'desc';
    onClick: () => void;
    children: React.ReactNode;
}> = ({ active, order, onClick, children }) => (
    <th
        className={`px-3 py-2 text-left cursor-pointer hover:bg-surface select-none ${TABLE_TH_TYPE}`}
        onClick={onClick}
    >
        <span className="inline-flex items-center gap-1">
            {children}
            {active && (order === 'asc' ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />)}
        </span>
    </th>
);

export default VideoGenJobsTab;

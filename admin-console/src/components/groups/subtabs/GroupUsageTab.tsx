/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useCallback } from 'react';
import { TABLE_TH_TYPE } from '../../ui/tableStyles';
import { useTimespanPreference } from '../../../hooks/useTimespanPreference';
import {
    Users,
    Brain,
    Bot,
    TrendingUp,
    Layers,
    ChevronRight,
    Activity,
    BarChart3,
    CheckCircle,
    XCircle,
    Loader,
    AlertCircle,
    RefreshCw,
    DollarSign,
    Zap,
    Hash,
    Wrench,
    Sparkles,
    GitBranch,
    Image as ImageIcon,
    Mic,
    Speaker,
    Video,
} from 'lucide-react';
import MCPIcon from '../../common/MCPIcon';
import { GroupType, ResourceGroup, GroupUsageResponse, UsagePeriod, GroupUsageQueryParams } from '../../../lib/groupTypes';
import { bucketDaily } from '../../../lib/timeBuckets';
import { useGateway } from '../../../hooks/useGateway';
import { errorKind, errorMessage } from '../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';

interface Props {
    group: ResourceGroup;
}

const PERIOD_OPTIONS: { value: UsagePeriod; label: string }[] = [
    { value: '24h', label: '24 Hours' },
    { value: '7d', label: '7 Days' },
    { value: '30d', label: '30 Days' },
    { value: '90d', label: '90 Days' },
    { value: 'all', label: 'All Time' },
];

const PERIOD_VALUES = PERIOD_OPTIONS.map((o) => o.value);

/** Format a number with commas */
const fmt = (n: number): string => n.toLocaleString();

/** Compact display for token-scale numbers: 618,842,142 reads as
 *  "618.8M" on a stat tile. Below one million the full grouped number
 *  stays — 54,323 is still scannable, and over-compacting small counts
 *  loses precision operators actually use. Full value belongs in a
 *  title/tooltip at the call site when it matters. */
const fmtCompact = (n: number): string => {
    if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
    if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
    return n.toLocaleString();
};

/** Format cost to appropriate precision */
const fmtCost = (n: number): string => {
    if (n === 0) return '$0.00';
    if (n < 0.01) return `$${n.toFixed(6)}`;
    if (n < 1) return `$${n.toFixed(4)}`;
    return `$${n.toFixed(2)}`;
};

/** Format bytes to human-readable */
const fmtBytes = (bytes: number): string => {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
};

/** Format milliseconds to human-readable */
const fmtMs = (ms: number): string => {
    if (ms < 1000) return `${Math.round(ms)}ms`;
    return `${(ms / 1000).toFixed(1)}s`;
};

/** Adaptive time-bucketing for the activity charts.
 *
 *  At 24h–30d a bar per day reads fine; at 90 days ninety 3px slivers
 *  per mini-chart are unreadable, and All-Time is worse. Roll daily
 *  rows up to weeks past ~5 weeks of data, and to months past ~26
 *  weeks. Bucket keys stay ISO dates (week = its Monday, month =
 *  YYYY-MM) so the hover panel and range labels keep working
 *  unchanged. */
type ActivityRow = {
    date: string;
    llm_requests: number;
    mcp_requests: number;
    a2a_requests?: number;
    skill_requests?: number;
    total_tokens: number;
    total_cost: number;
    error_count: number;
};

// The day→week→month grain rule + rollup live in lib/timeBuckets so RG
// Usage and Mission Control's cost composition can never disagree about
// when bars become weekly.
const bucketActivity = (rows: ActivityRow[]): { grain: 'Daily' | 'Weekly' | 'Monthly'; rows: ActivityRow[] } =>
    bucketDaily(rows);

/** Simple inline bar chart for daily breakdown.
 *  When `maxValue` is provided, the chart scales against that
 *  (lets sibling charts share a y-axis so e.g. 3 Agent requests
 *  don't visually equal 149 LLM requests). Without it, falls back
 *  to the chart's own max — the right call for cost charts where
 *  the unit differs from sibling Request charts. */
const MiniBarChart: React.FC<{
    data: { date: string; value: number }[];
    color: string;
    height?: number;
    hoveredIndex?: number | null;
    onHoverIndex?: (index: number | null) => void;
    maxValue?: number;
}> = ({ data, color, height = 64, hoveredIndex, onHoverIndex, maxValue }) => {
    const localMax = Math.max(...data.map(d => d.value), 1);
    const max = maxValue !== undefined && maxValue > 0 ? maxValue : localMax;

    return (
        <div
            className="flex items-end gap-px"
            style={{ height }}
            onMouseLeave={() => onHoverIndex?.(null)}
        >
            {data.map((d, i) => {
                const h = Math.max((d.value / max) * height, 2);
                const isHovered = hoveredIndex === i;
                const isDimmed = hoveredIndex !== null && hoveredIndex !== undefined && !isHovered;
                return (
                    <div
                        key={i}
                        className="flex-1 rounded-t-sm transition-all cursor-pointer min-h-0 min-w-0"
                        style={{
                            height: h,
                            backgroundColor: color,
                            minWidth: 3,
                            opacity: isDimmed ? 0.3 : 1,
                            transform: isHovered ? 'scaleY(1.05)' : 'scaleY(1)',
                            transformOrigin: 'bottom',
                        }}
                        onMouseEnter={() => onHoverIndex?.(i)}
                    />
                );
            })}
        </div>
    );
};

const GroupUsageTab: React.FC<Props> = ({ group }) => {
    const { fetchGroupUsage } = useGateway();
    const stats = group.stats;

    // Usage data state
    const [usage, setUsage] = useState<GroupUsageResponse | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    // Shared with Mission Control / Analytics / AI Systems; '30d' stays
    // this surface's fallback so nothing changes until the admin picks.
    const [period, setPeriod] = useTimespanPreference(PERIOD_VALUES, '30d');
    // Organizational groups (anything that is not an AI System) default to
    // aggregated usage: an org node's own direct usage is usually near
    // zero, and a page of zeros under a group holding twenty systems
    // reads as "nothing happening" when the estate is busy.
    const [includeHierarchy, setIncludeHierarchy] = useState(
        group.group_type !== GroupType.AI_SYSTEM
    );
    // The panel keeps one instance across group navigation, so re-derive
    // the default whenever the group changes — an org group must open
    // aggregated however the operator got here.
    useEffect(() => {
        setIncludeHierarchy(group.group_type !== GroupType.AI_SYSTEM);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [group.id]);
    const [hoveredDayIndex, setHoveredDayIndex] = useState<number | null>(null);

    const loadUsage = useCallback(async () => {
        try {
            setLoading(true);
            setError('');
            const params: GroupUsageQueryParams = {
                period,
                include_hierarchy: includeHierarchy,
            };
            const data = await fetchGroupUsage(group.id, params);
            setUsage(data);
        } catch (err) {
            console.error('Failed to load usage data:', errorKind(err));
            setError(errorMessage(err, 'Failed to load usage data'));
        } finally {
            setLoading(false);
        }
    }, [group.id, period, includeHierarchy, fetchGroupUsage]);

    useEffect(() => {
        runBackground(loadUsage(), 'usage');
    }, [loadUsage]);

    return (
        <div className="p-6 space-y-6">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div>
                    <h2 className="text-lg font-semibold text-default">Usage & Analytics</h2>
                    <p className="text-sm text-secondary mt-1">
                        Resource usage, costs, and metrics for this group
                    </p>
                </div>
                <button
                    onClick={asyncHandler(loadUsage, 'click')}
                    disabled={loading}
                    className="p-2 text-secondary hover:text-default hover:bg-surface-raised rounded-lg transition-colors disabled:opacity-50"
                    title="Refresh usage data"
                >
                    <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
                </button>
            </div>

            {/* ============================================================ */}
            {/* USAGE ANALYTICS SECTION                                      */}
            {/* ============================================================ */}

            {/* Period Controls */}
            <div className="bg-surface border border-default rounded-lg p-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                    <div className="flex items-center gap-2">
                        <span className="text-sm font-medium text-secondary">Period:</span>
                        <div className="flex bg-surface-raised rounded-lg p-0.5">
                            {PERIOD_OPTIONS.map((opt) => (
                                <button
                                    key={opt.value}
                                    onClick={() => setPeriod(opt.value)}
                                    className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
                                        period === opt.value
                                            ? 'bg-surface text-default shadow-sm'
                                            : 'text-secondary hover:text-default'
                                    }`}
                                >
                                    {opt.label}
                                </button>
                            ))}
                        </div>
                    </div>

                    {/* Hierarchy toggle */}
                    {(stats?.direct_children_count || 0) > 0 && (
                        <label className="flex items-center gap-2 cursor-pointer">
                            <input
                                type="checkbox"
                                checked={includeHierarchy}
                                onChange={(e) => setIncludeHierarchy(e.target.checked)}
                                className="w-4 h-4 rounded border-strong text-primary-600 focus:ring-primary-500"
                            />
                            <span className="text-sm text-secondary">
                                <GitBranch className="w-3.5 h-3.5 inline mr-1" />
                                Include descendant groups
                            </span>
                        </label>
                    )}
                </div>
            </div>

            {/* Loading / Error states */}
            {loading && !usage && (
                <div className="flex items-center justify-center py-12">
                    <Loader className="w-6 h-6 text-primary-600 animate-spin mr-2" />
                    <span className="text-sm text-secondary">Loading usage data...</span>
                </div>
            )}

            {error && (
                <div className="flex items-start space-x-3 text-red-600 bg-surface-alt rounded-lg p-4 border border-default">
                    <AlertCircle className="w-5 h-5 flex-shrink-0 mt-0.5" />
                    <div>
                        <p className="text-sm font-medium">Failed to load usage data</p>
                        <p className="text-sm mt-1">{error}</p>
                    </div>
                </div>
            )}

            {/* Usage Data */}
            {usage && (
                <>
                    {/* Summary Row.
                        7 cards on lg+ so the three resource-type
                        counts (LLM Tokens, MCP Calls, Agent Calls)
                        sit alongside Total/Cost/Users/Errors without
                        wrapping. md collapses to 4-up so the cards
                        breathe a little; phones get 2-up. */}
                    <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-8 gap-4">
                        <SummaryCard
                            icon={<Hash className="w-4 h-4" />}
                            label="Total Requests"
                            value={fmt(usage.summary.total_requests)}
                            iconBg="bg-primary-pale"
                            iconColor="text-primary"
                        />
                        <SummaryCard
                            icon={<DollarSign className="w-4 h-4" />}
                            label="Total Cost"
                            value={fmtCost(usage.summary.total_cost)}
                            iconBg="bg-surface-alt"
                            iconColor="text-green-600"
                        />
                        <SummaryCard
                            icon={<Zap className="w-4 h-4" />}
                            label="LLM Tokens"
                            value={fmtCompact(usage.llm.total_tokens)}
                            iconBg="bg-surface-alt"
                            iconColor="text-muted"
                        />
                        <SummaryCard
                            icon={<Wrench className="w-4 h-4" />}
                            label="MCP Calls"
                            value={fmt(usage.mcp.total_tool_calls)}
                            iconBg="bg-surface-alt"
                            iconColor="text-muted"
                        />
                        <SummaryCard
                            icon={<Bot className="w-4 h-4" />}
                            label="Agent Calls"
                            value={fmt(usage.a2a?.request_count ?? 0)}
                            iconBg="bg-surface-alt"
                            iconColor="text-amber-600"
                        />
                        <SummaryCard
                            icon={<Sparkles className="w-4 h-4" />}
                            label="Skill Calls"
                            value={fmt(usage.skill?.request_count ?? 0)}
                            iconBg="bg-surface-alt"
                            iconColor="text-primary"
                        />
                        <SummaryCard
                            icon={<Users className="w-4 h-4" />}
                            label="Unique Users"
                            value={fmt(usage.summary.unique_users)}
                            iconBg="bg-surface-alt"
                            iconColor="text-muted"
                        />
                        <SummaryCard
                            icon={<XCircle className="w-4 h-4" />}
                            label="Error Rate"
                            value={`${usage.summary.error_rate}%`}
                            iconBg={usage.summary.error_rate > 5 ? 'bg-surface-alt' : 'bg-surface-alt'}
                            iconColor={usage.summary.error_rate > 5 ? 'text-red-600' : 'text-secondary'}
                        />
                    </div>

                    {/* Activity chart — daily bars up to ~a month of
                        data, then weekly, then monthly (90d/All-Time as
                        raw daily bars is an unreadable picket fence). */}
                    {usage.daily_breakdown.length > 0 && (() => {
                        const activity = bucketActivity(usage.daily_breakdown);
                        return (
                        <div className="bg-surface border border-default rounded-lg p-6">
                            <h3 className="text-sm font-semibold text-default mb-4 flex items-center space-x-2">
                                <Activity className="w-4 h-4 text-secondary" />
                                <span>{activity.grain} Activity</span>
                                {activity.grain !== 'Daily' && (
                                    <span className="text-xs text-muted font-normal">
                                        (per {activity.grain === 'Weekly' ? 'week' : 'month'})
                                    </span>
                                )}
                            </h3>

                            {(() => {
                                // Shared y-axis ceiling for the three Request
                                // charts so 3 Agent calls don't visually equal
                                // 149 LLM calls. Daily Cost uses its own scale
                                // (different unit) and is left out of the max.
                                const requestsMax = Math.max(
                                    1,
                                    ...activity.rows.map(d => d.llm_requests),
                                    ...activity.rows.map(d => d.mcp_requests),
                                    ...activity.rows.map(d => d.a2a_requests ?? 0),
                                );
                                return (
                            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
                                <div>
                                    <p className="text-xs text-muted mb-2">Requests (LLM)</p>
                                    <MiniBarChart
                                        data={activity.rows.map(d => ({
                                            date: d.date,
                                            value: d.llm_requests
                                        }))}
                                        color="#8b5cf6"
                                        hoveredIndex={hoveredDayIndex}
                                        onHoverIndex={setHoveredDayIndex}
                                        maxValue={requestsMax}
                                    />
                                </div>
                                <div>
                                    <p className="text-xs text-muted mb-2">Requests (MCP)</p>
                                    <MiniBarChart
                                        data={activity.rows.map(d => ({
                                            date: d.date,
                                            value: d.mcp_requests
                                        }))}
                                        color="#f97316"
                                        hoveredIndex={hoveredDayIndex}
                                        onHoverIndex={setHoveredDayIndex}
                                        maxValue={requestsMax}
                                    />
                                </div>
                                <div>
                                    <p className="text-xs text-muted mb-2">Requests (Agent)</p>
                                    <MiniBarChart
                                        data={activity.rows.map(d => ({
                                            date: d.date,
                                            value: d.a2a_requests ?? 0
                                        }))}
                                        color="#d97706"
                                        hoveredIndex={hoveredDayIndex}
                                        onHoverIndex={setHoveredDayIndex}
                                        maxValue={requestsMax}
                                    />
                                </div>
                                <div>
                                    <p className="text-xs text-muted mb-2">{activity.grain} Cost</p>
                                    <MiniBarChart
                                        data={activity.rows.map(d => ({
                                            date: d.date,
                                            value: d.total_cost
                                        }))}
                                        color="#22c55e"
                                        hoveredIndex={hoveredDayIndex}
                                        onHoverIndex={setHoveredDayIndex}
                                    />
                                </div>
                            </div>
                                );
                            })()}

                            {/* Date range labels */}
                            <div className="flex justify-between mt-2 text-xs text-muted">
                                <span>{activity.rows[0]?.date}</span>
                                <span>{activity.rows[activity.rows.length - 1]?.date}</span>
                            </div>

                            {/* Hovered day detail */}
                            {hoveredDayIndex !== null && activity.rows[hoveredDayIndex] && (() => {
                                const day = activity.rows[hoveredDayIndex];
                                const a2aRequests = day.a2a_requests ?? 0;
                                const totalRequests = day.llm_requests + day.mcp_requests + a2aRequests;
                                return (
                                    <div className="mt-4 p-3 bg-surface-alt border border-default rounded-lg grid grid-cols-2 md:grid-cols-7 gap-3 text-center animate-in fade-in duration-150">
                                        <div>
                                            <p className="text-xs text-muted">Date</p>
                                            <p className="text-sm font-semibold text-default">{day.date}</p>
                                        </div>
                                        <div>
                                            <p className="text-xs text-muted">LLM Requests</p>
                                            <p className="text-sm font-semibold text-muted">{fmt(day.llm_requests)}</p>
                                        </div>
                                        <div>
                                            <p className="text-xs text-muted">MCP Requests</p>
                                            <p className="text-sm font-semibold text-muted">{fmt(day.mcp_requests)}</p>
                                        </div>
                                        <div>
                                            <p className="text-xs text-muted">Agent Requests</p>
                                            <p className="text-sm font-semibold text-amber-600">{fmt(a2aRequests)}</p>
                                        </div>
                                        <div>
                                            <p className="text-xs text-muted">Tokens</p>
                                            <p className="text-sm font-semibold text-default">{fmtCompact(day.total_tokens)}</p>
                                        </div>
                                        <div>
                                            <p className="text-xs text-muted">Cost</p>
                                            <p className="text-sm font-semibold text-green-600">{fmtCost(day.total_cost)}</p>
                                        </div>
                                        <div>
                                            <p className="text-xs text-muted">Errors</p>
                                            <p className={`text-sm font-semibold ${day.error_count > 0 ? 'text-red-600' : 'text-muted'}`}>
                                                {day.error_count > 0
                                                    ? `${day.error_count} (${totalRequests > 0 ? ((day.error_count / totalRequests) * 100).toFixed(1) : 0}%)`
                                                    : 'None'
                                                }
                                            </p>
                                        </div>
                                    </div>
                                );
                            })()}
                        </div>
                        );
                    })()}

                    {/* LLM, MCP & Agent Detail Panels.
                        Three-up on one row using a 12-column grid:
                          - LLM Usage:    5 cols (or 7 if no Agent)
                          - MCP Usage:    4 cols (or 5 if no Agent)
                          - Agent Usage:  3 cols (only if a2a present)
                        LLM Usage internally splits Completion + Media
                        into two sub-sections so chat-token and media-
                        unit metrics share one panel — they're both
                        "LLM" from a billing perspective. The Models-
                        used list at the bottom spans the panel width
                        and surfaces every model regardless of mode
                        (`usage.llm.top_models` already includes media
                        rows because record_multimodal_outcome writes
                        resource_type='llm' into usage_logs).
                        Single column on smaller widths. */}
                    {(() => {
                        const hasMedia = !!usage.media && usage.media.request_count > 0;
                        const llmSpan = usage.a2a ? 'lg:col-span-5' : 'lg:col-span-7';
                        const mcpSpan = usage.a2a ? 'lg:col-span-4' : 'lg:col-span-5';
                        return (
                    <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">

                        {/* LLM Usage Panel — combined Completion + Media */}
                        <div className={`bg-surface border border-default rounded-lg p-6 ${llmSpan}`}>
                            <h3 className="text-sm font-semibold text-default mb-4 flex items-center space-x-2">
                                <Brain className="w-4 h-4 text-muted" />
                                <span>LLM Usage</span>
                                <span className="ml-auto text-xs font-normal text-muted">
                                    {fmt(usage.llm.request_count)} requests
                                </span>
                            </h3>

                            {usage.llm.request_count === 0 ? (
                                <p className="text-sm text-muted py-4 text-center">No LLM usage in this period</p>
                            ) : (
                                <div className="space-y-4">
                                    {/* Side-by-side sub-sections.
                                        Completion holds the token
                                        breakdown (chat / completion /
                                        embedding); Media holds the
                                        typed billing-unit counters for
                                        image / TTS / STT / video that
                                        the token columns can't
                                        represent. Stacks vertically on
                                        narrow widths. */}
                                    <div className={`grid gap-4 ${hasMedia ? 'grid-cols-1 xl:grid-cols-2' : 'grid-cols-1'}`}>

                                        {/* Completion sub-section. We
                                            check total_tokens > 0
                                            instead of request_count
                                            because media rows are
                                            included in `usage.llm` but
                                            never carry tokens — this
                                            keeps the Completion column
                                            from showing "0 tokens"
                                            tiles when only media
                                            traffic is in scope. */}
                                        {usage.llm.total_tokens > 0 && (
                                            <div className="space-y-3">
                                                <p className={TABLE_TH_TYPE}>
                                                    Completion
                                                </p>
                                                <div className="grid grid-cols-3 gap-2">
                                                    <MetricBox label="Prompt" value={fmtCompact(usage.llm.prompt_tokens)} sub={fmtCost(usage.llm.prompt_cost)} />
                                                    <MetricBox label="Completion" value={fmtCompact(usage.llm.completion_tokens)} sub={fmtCost(usage.llm.completion_cost)} />
                                                    <MetricBox label="Total" value={fmtCompact(usage.llm.total_tokens)} sub={fmtCost(usage.llm.total_cost)} highlight />
                                                </div>
                                                <div className="grid grid-cols-3 gap-x-3 text-xs">
                                                    <div className="flex items-center justify-between">
                                                        <span className="text-muted">Avg tok/req</span>
                                                        <span className="font-medium text-default">{fmt(Math.round(usage.llm.avg_tokens_per_request))}</span>
                                                    </div>
                                                    <div className="flex items-center justify-between">
                                                        <span className="text-muted">Avg ms</span>
                                                        <span className="font-medium text-default">{fmtMs(usage.llm.avg_response_time_ms)}</span>
                                                    </div>
                                                    <div className="flex items-center justify-between">
                                                        <span className="text-muted">Success</span>
                                                        <span className={`font-medium ${
                                                            usage.llm.error_count > 0 ? 'text-muted' : 'text-green-600'
                                                        }`}>
                                                            {usage.llm.request_count > 0
                                                                ? `${((usage.llm.success_count / usage.llm.request_count) * 100).toFixed(1)}%`
                                                                : '—'}
                                                        </span>
                                                    </div>
                                                </div>
                                            </div>
                                        )}

                                        {/* Media sub-section. Iterates
                                            the canonical mode order so
                                            the layout stays stable
                                            across refreshes
                                            (Object.entries order isn't
                                            contractually stable). */}
                                        {hasMedia && (
                                            <div className="space-y-3">
                                                <p className={TABLE_TH_TYPE}>
                                                    Media
                                                </p>
                                                <div className="grid grid-cols-2 gap-2">
                                                    {([
                                                        { key: 'image_generation', label: 'Images', unit: 'images', icon: ImageIcon, counterKey: 'images_generated' as const, format: (n: number) => fmt(n) },
                                                        { key: 'audio_speech', label: 'Speech', unit: 'chars', icon: Speaker, counterKey: 'audio_chars' as const, format: (n: number) => fmt(n) },
                                                        { key: 'audio_transcription', label: 'Transcribe', unit: 'sec', icon: Mic, counterKey: 'audio_seconds_input' as const, format: (n: number) => `${n.toFixed(1)}s` },
                                                        { key: 'video_generation', label: 'Video', unit: 'sec', icon: Video, counterKey: 'video_seconds_output' as const, format: (n: number) => `${n.toFixed(1)}s` },
                                                    ] as const).map(({ key, label, unit, icon: Icon, counterKey, format }) => {
                                                        const m = usage.media!.modes[key];
                                                        if (!m) return null;
                                                        const counter = Number(m[counterKey] ?? 0);
                                                        return (
                                                            <div key={key} className="bg-surface-alt rounded-md border border-default p-2">
                                                                <div className="flex items-center gap-1.5 mb-0.5 text-xs text-muted">
                                                                    <Icon className="w-3.5 h-3.5" />
                                                                    <span>{label}</span>
                                                                </div>
                                                                <div className="text-sm font-semibold text-default">
                                                                    {format(counter)}
                                                                </div>
                                                                <div className="flex items-center justify-between mt-0.5 text-xs text-muted">
                                                                    <span>{fmt(m.request_count)} · {unit}</span>
                                                                    {m.cost_usd > 0 && (
                                                                        <span className="text-green-700 font-medium">
                                                                            {fmtCost(m.cost_usd)}
                                                                        </span>
                                                                    )}
                                                                </div>
                                                            </div>
                                                        );
                                                    })}
                                                </div>
                                            </div>
                                        )}
                                    </div>

                                    {/* Models used — spans the full
                                        panel width below both sub-
                                        sections. Includes media models
                                        too because record_multimodal_outcome
                                        writes resource_type='llm' rows. */}
                                    {usage.llm.top_models.length > 0 && (
                                        <div className="pt-3 border-t border-default">
                                            <div className="flex items-center justify-between mb-2">
                                                <p className={TABLE_TH_TYPE}>
                                                    Models used
                                                </p>
                                                <span className="text-xs text-muted">
                                                    {usage.llm.top_models.length} model{usage.llm.top_models.length !== 1 ? 's' : ''}
                                                </span>
                                            </div>
                                            <div className="space-y-2">
                                                {/* Most-called model on top, whatever
                                                    order the list arrived in. */}
                                                {[...usage.llm.top_models]
                                                    .sort((a, b) => (b.request_count - a.request_count) || (b.total_tokens - a.total_tokens))
                                                    .map((model, idx) => (
                                                    <div key={idx} className="flex items-center justify-between text-sm">
                                                        <div className="flex items-center gap-2 min-w-0">
                                                            <span className="w-5 text-xs text-muted">{idx + 1}.</span>
                                                            <span className="text-default truncate font-medium">
                                                                {model.model_name}
                                                            </span>
                                                            {model.provider && (
                                                                <span className="text-xs px-1.5 py-0.5 rounded bg-surface-raised text-muted flex-shrink-0">
                                                                    {model.provider}
                                                                </span>
                                                            )}
                                                        </div>
                                                        <div className="flex items-center gap-3 flex-shrink-0 ml-2">
                                                            <span className="text-default font-medium">{fmt(model.request_count)} calls</span>
                                                            <span className="text-muted text-xs">{fmtCompact(model.total_tokens)} tok</span>
                                                            <span className="text-default font-medium">{fmtCost(model.total_cost)}</span>
                                                        </div>
                                                    </div>
                                                ))}
                                            </div>
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>

                        {/* MCP Usage Panel */}
                        <div className={`bg-surface border border-default rounded-lg p-6 ${mcpSpan}`}>
                            <h3 className="text-sm font-semibold text-default mb-4 flex items-center space-x-2">
                                <MCPIcon className="w-4 h-4 text-muted" />
                                <span>MCP Usage</span>
                                <span className="ml-auto text-xs font-normal text-muted">
                                    {fmt(usage.mcp.request_count)} requests
                                </span>
                            </h3>

                            {usage.mcp.request_count === 0 ? (
                                <p className="text-sm text-muted py-4 text-center">No MCP usage in this period</p>
                            ) : (
                                <div className="space-y-4">
                                    {/* MCP summary metrics */}
                                    <div className="grid grid-cols-3 gap-3">
                                        <MetricBox label="Tool Calls" value={fmt(usage.mcp.total_tool_calls)} />
                                        <MetricBox label="Data Transfer" value={fmtBytes(usage.mcp.total_data_bytes)} />
                                        <MetricBox label="Total Cost" value={fmtCost(usage.mcp.total_cost)} highlight />
                                    </div>

                                    {/* Performance */}
                                    <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm py-2 border-t border-default">
                                        <div className="flex items-center justify-between">
                                            <span className="text-muted">Avg execution time</span>
                                            <span className="font-medium text-default">{fmtMs(usage.mcp.avg_execution_time_ms)}</span>
                                        </div>
                                        <div className="flex items-center justify-between">
                                            <span className="text-muted">Success rate</span>
                                            <span className={`font-medium ${
                                                usage.mcp.error_count > 0 ? 'text-muted' : 'text-green-600'
                                            }`}>
                                                {usage.mcp.request_count > 0
                                                    ? `${((usage.mcp.success_count / usage.mcp.request_count) * 100).toFixed(1)}%`
                                                    : '—'}
                                            </span>
                                        </div>
                                    </div>

                                    {/* Tool Calls & System Methods — split */}
                                    {usage.mcp.top_tools.length > 0 && (() => {
                                        const MCP_SYSTEM_METHODS = ['initialize', 'tools/list', 'tools/call', 'resources/list',
                                            'resources/read', 'resources/templates/list', 'prompts/list', 'prompts/get',
                                            'notifications/initialized', 'completion/complete', 'logging/setLevel', 'ping', 'unknown'];
                                        // Busiest tool / method at the top of each
                                        // list, whatever order top_tools arrived in.
                                        const sortByCalls = <T extends { call_count: number }>(arr: T[]): T[] =>
                                            [...arr].sort((a, b) => b.call_count - a.call_count);
                                        const toolCalls = sortByCalls(
                                            usage.mcp.top_tools.filter(t => !MCP_SYSTEM_METHODS.includes(t.tool_name))
                                        );
                                        const systemMethods = sortByCalls(
                                            usage.mcp.top_tools.filter(t => MCP_SYSTEM_METHODS.includes(t.tool_name))
                                        );

                                        const renderList = (items: typeof toolCalls, showServer: boolean) => (
                                            <div className="space-y-1.5 max-h-44 overflow-y-auto pr-1">
                                                {items.map((tool, idx) => (
                                                    <div key={idx} className="flex items-center justify-between text-sm">
                                                        <div className="flex items-center gap-2 min-w-0">
                                                            <span className="w-5 text-xs text-muted">{idx + 1}.</span>
                                                            <span className="text-default truncate font-medium">
                                                                {tool.tool_name}
                                                                {showServer && tool.server_name && (
                                                                    <span className="text-muted font-normal"> ({tool.server_name})</span>
                                                                )}
                                                            </span>
                                                        </div>
                                                        <div className="flex items-center gap-3 flex-shrink-0 ml-2">
                                                            <span className="text-muted text-xs">
                                                                {fmt(tool.call_count)} calls
                                                            </span>
                                                            <span className="text-muted text-xs">
                                                                {fmtMs(tool.total_execution_time_ms)}
                                                            </span>
                                                            {tool.error_count > 0 && (
                                                                <span className="text-xs px-1.5 py-0.5 rounded bg-surface-alt text-red-600">
                                                                    {tool.error_count} err
                                                                </span>
                                                            )}
                                                        </div>
                                                    </div>
                                                ))}
                                            </div>
                                        );

                                        return (
                                            <div className="pt-3 border-t border-default space-y-4">
                                                {/* Tool Calls */}
                                                {toolCalls.length > 0 && (
                                                    <div>
                                                        <div className="flex items-center justify-between mb-2">
                                                            <p className={TABLE_TH_TYPE}>
                                                                Tool Calls
                                                            </p>
                                                            <span className="text-xs text-muted">
                                                                {toolCalls.length} tool{toolCalls.length !== 1 ? 's' : ''}
                                                            </span>
                                                        </div>
                                                        {renderList(toolCalls, true)}
                                                    </div>
                                                )}

                                                {/* System Methods */}
                                                {systemMethods.length > 0 && (
                                                    <div>
                                                        <div className="flex items-center justify-between mb-2">
                                                            <p className={TABLE_TH_TYPE}>
                                                                System Methods
                                                            </p>
                                                            <span className="text-xs text-muted">
                                                                {systemMethods.length} method{systemMethods.length !== 1 ? 's' : ''}
                                                            </span>
                                                        </div>
                                                        {renderList(systemMethods, false)}
                                                    </div>
                                                )}
                                            </div>
                                        );
                                    })()}
                                </div>
                            )}
                        </div>

                        {/* Agent Usage Panel — A2A traffic. Currently
                            sourced directly from proxy_logs
                            (proxy_type='a2a') since A2A doesn't yet
                            land in usage_logs; cost stays at zero
                            until an A2A pricing model exists. */}
                        {usage.a2a && (
                        <div className="bg-surface border border-default rounded-lg p-6 lg:col-span-3">
                            <h3 className="text-sm font-semibold text-default mb-4 flex items-center space-x-2">
                                <Bot className="w-4 h-4 text-amber-600" />
                                <span>Agent Usage</span>
                                <span className="ml-auto text-xs font-normal text-muted">
                                    {fmt(usage.a2a.request_count)} requests
                                </span>
                            </h3>

                            {usage.a2a.request_count === 0 ? (
                                <p className="text-sm text-muted py-4 text-center">No A2A usage in this period</p>
                            ) : (
                                <div className="space-y-4">
                                    <div className="grid grid-cols-3 gap-3">
                                        <MetricBox
                                            label="Capability calls"
                                            value={fmt(usage.a2a.top_capabilities.reduce((s, c) => s + c.call_count, 0))}
                                        />
                                        <MetricBox label="Data Transfer" value={fmtBytes(usage.a2a.total_data_bytes)} />
                                        <MetricBox label="Avg duration" value={fmtMs(usage.a2a.avg_duration_ms)} />
                                    </div>

                                    <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm py-2 border-t border-default">
                                        <div className="flex items-center justify-between">
                                            <span className="text-muted">Successful</span>
                                            <span className="font-medium text-default">{fmt(usage.a2a.success_count)}</span>
                                        </div>
                                        <div className="flex items-center justify-between">
                                            <span className="text-muted">Success rate</span>
                                            <span className={`font-medium ${
                                                usage.a2a.error_count > 0 ? 'text-muted' : 'text-green-600'
                                            }`}>
                                                {usage.a2a.request_count > 0
                                                    ? `${((usage.a2a.success_count / usage.a2a.request_count) * 100).toFixed(1)}%`
                                                    : '—'}
                                            </span>
                                        </div>
                                    </div>

                                    {usage.a2a.top_cards.length > 0 && (
                                        <div className="pt-3 border-t border-default">
                                            <div className="flex items-center justify-between mb-2">
                                                <p className={TABLE_TH_TYPE}>
                                                    Agent Cards
                                                </p>
                                                <span className="text-xs text-muted">
                                                    {usage.a2a.top_cards.length} card{usage.a2a.top_cards.length !== 1 ? 's' : ''}
                                                </span>
                                            </div>
                                            <div className="space-y-1.5 max-h-44 overflow-y-auto pr-1">
                                                {usage.a2a.top_cards.map((card, idx) => (
                                                    <div key={card.card_id} className="flex items-center justify-between text-sm">
                                                        <div className="flex items-center gap-2 min-w-0">
                                                            <span className="w-5 text-xs text-muted">{idx + 1}.</span>
                                                            <span className="text-default truncate font-medium">
                                                                {card.card_name}
                                                            </span>
                                                        </div>
                                                        <div className="flex items-center gap-3 flex-shrink-0 ml-2">
                                                            <span className="text-muted text-xs">
                                                                {fmt(card.request_count)} calls
                                                            </span>
                                                            <span className="text-muted text-xs">
                                                                {fmtMs(card.total_duration_ms)}
                                                            </span>
                                                            {card.error_count > 0 && (
                                                                <span className="text-xs px-1.5 py-0.5 rounded bg-surface-alt text-red-600">
                                                                    {card.error_count} err
                                                                </span>
                                                            )}
                                                        </div>
                                                    </div>
                                                ))}
                                            </div>
                                        </div>
                                    )}

                                    {usage.a2a.top_capabilities.length > 0 && (
                                        <div className="pt-3 border-t border-default">
                                            <div className="flex items-center justify-between mb-2">
                                                <p className={TABLE_TH_TYPE}>
                                                    Capabilities
                                                </p>
                                                <span className="text-xs text-muted">
                                                    {usage.a2a.top_capabilities.length} capabilit{usage.a2a.top_capabilities.length !== 1 ? 'ies' : 'y'}
                                                </span>
                                            </div>
                                            <div className="space-y-1.5 max-h-44 overflow-y-auto pr-1">
                                                {usage.a2a.top_capabilities.map((cap, idx) => (
                                                    <div key={`${cap.card_id ?? '_'}::${cap.capability_name}`} className="flex items-center justify-between text-sm">
                                                        <div className="flex items-center gap-2 min-w-0">
                                                            <span className="w-5 text-xs text-muted">{idx + 1}.</span>
                                                            <span className="text-default truncate font-medium">
                                                                {cap.capability_name}
                                                                {cap.card_name && (
                                                                    <span className="text-muted font-normal"> ({cap.card_name})</span>
                                                                )}
                                                            </span>
                                                        </div>
                                                        <div className="flex items-center gap-3 flex-shrink-0 ml-2">
                                                            <span className="text-muted text-xs">
                                                                {fmt(cap.call_count)} calls
                                                            </span>
                                                            {cap.error_count > 0 && (
                                                                <span className="text-xs px-1.5 py-0.5 rounded bg-surface-alt text-red-600">
                                                                    {cap.error_count} err
                                                                </span>
                                                            )}
                                                        </div>
                                                    </div>
                                                ))}
                                            </div>
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>
                        )}

                        {/* Skill Usage Panel — Agent-Skill executions
                            (usage_logs resource_type='skill'). One row per
                            call; cost is zero until a skill pricing model
                            exists. */}
                        {usage.skill && (
                        <div className="bg-surface border border-default rounded-lg p-6 lg:col-span-3">
                            <h3 className="text-sm font-semibold text-default mb-4 flex items-center space-x-2">
                                <Sparkles className="w-4 h-4 text-primary" />
                                <span>Skill Usage</span>
                                <span className="ml-auto text-xs font-normal text-muted">
                                    {fmt(usage.skill.request_count)} calls
                                </span>
                            </h3>

                            {usage.skill.request_count === 0 ? (
                                <p className="text-sm text-muted py-4 text-center">No skill usage in this period</p>
                            ) : (
                                <div className="space-y-4">
                                    <div className="grid grid-cols-3 gap-3">
                                        <MetricBox label="Executions" value={fmt(usage.skill.request_count)} />
                                        <MetricBox label="Successful" value={fmt(usage.skill.success_count)} />
                                        <MetricBox label="Avg duration" value={fmtMs(usage.skill.avg_execution_time_ms)} />
                                    </div>

                                    {usage.skill.top_skills.length > 0 && (
                                        <div className="pt-3 border-t border-default">
                                            <div className="flex items-center justify-between mb-2">
                                                <p className={TABLE_TH_TYPE}>
                                                    Skills
                                                </p>
                                                <span className="text-xs text-muted">
                                                    {usage.skill.top_skills.length} skill{usage.skill.top_skills.length !== 1 ? 's' : ''}
                                                </span>
                                            </div>
                                            <div className="space-y-1.5 max-h-44 overflow-y-auto pr-1">
                                                {usage.skill.top_skills.map((sk, idx) => (
                                                    <div key={sk.skill_id ?? sk.skill_name} className="flex items-center justify-between text-sm">
                                                        <div className="flex items-center gap-2 min-w-0">
                                                            <span className="w-5 text-xs text-muted">{idx + 1}.</span>
                                                            <span className="text-default truncate font-medium">
                                                                {sk.skill_name}
                                                            </span>
                                                        </div>
                                                        <div className="flex items-center gap-3 flex-shrink-0 ml-2">
                                                            <span className="text-muted text-xs">
                                                                {fmt(sk.call_count)} calls
                                                            </span>
                                                            <span className="text-muted text-xs">
                                                                {fmtMs(sk.total_execution_time_ms)}
                                                            </span>
                                                            {sk.error_count > 0 && (
                                                                <span className="text-xs px-1.5 py-0.5 rounded bg-surface-alt text-red-600">
                                                                    {sk.error_count} err
                                                                </span>
                                                            )}
                                                        </div>
                                                    </div>
                                                ))}
                                            </div>
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>
                        )}
                    </div>
                        );
                    })()}

                    {/* Hierarchy Roll-up Info (when hierarchy is enabled) */}
                    {includeHierarchy && usage.summary.descendant_groups > 0 && (
                        <div className="bg-primary-50 border border-default rounded-lg p-4">
                            <div className="flex items-center space-x-3">
                                <GitBranch className="w-5 h-5 text-primary-600" />
                                <div>
                                    <p className="text-sm font-medium text-default">
                                        Hierarchical Roll-up Active
                                    </p>
                                    <p className="text-sm text-secondary">
                                        Usage includes {usage.summary.descendant_groups} descendant
                                        {usage.summary.descendant_groups !== 1 ? ' groups' : ' group'} beneath
                                        {' '}<span className="font-medium">{usage.group_name}</span>
                                    </p>
                                </div>
                            </div>
                        </div>
                    )}

                    {/* No data state */}
                    {usage.summary.total_requests === 0 && (
                        <div className="text-center py-12 bg-surface-alt rounded-lg border-2 border-dashed border-strong">
                            <Activity className="w-12 h-12 text-muted mx-auto mb-3" />
                            <p className="text-default font-medium mb-1">No usage data for this period</p>
                            <p className="text-sm text-muted mt-2">
                                Usage will appear here once requests are made through this group's resources
                            </p>
                        </div>
                    )}
                </>
            )}

            {/* ============================================================ */}
            {/* HIERARCHY & RESOURCE INFO (existing sections, condensed)      */}
            {/* ============================================================ */}

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                {/* Hierarchy Information */}
                <div className="bg-surface border border-default rounded-lg p-6">
                    <h3 className="text-sm font-semibold text-default mb-4 flex items-center space-x-2">
                        <Layers className="w-4 h-4 text-secondary" />
                        <span>Hierarchy Information</span>
                    </h3>

                    <div className="space-y-3">
                        <InfoRow icon={<Activity className="w-4 h-4 text-secondary" />} label="Hierarchy Level" value={`Level ${group.hierarchy_level}`} />
                        {stats?.direct_children_count !== undefined && (
                            <InfoRow
                                icon={<Layers className="w-4 h-4 text-primary" />}
                                label="Direct Children"
                                value={`${stats.direct_children_count} group${stats.direct_children_count !== 1 ? 's' : ''}`}
                            />
                        )}
                        {stats?.total_descendants_count !== undefined && (
                            <InfoRow
                                icon={<BarChart3 className="w-4 h-4 text-muted" />}
                                label="Total Descendants"
                                value={`${stats.total_descendants_count} group${stats.total_descendants_count !== 1 ? 's' : ''}`}
                            />
                        )}
                        <InfoRow
                            icon={group.is_active
                                ? <CheckCircle className="w-4 h-4 text-green-600" />
                                : <XCircle className="w-4 h-4 text-muted" />}
                            label="Status"
                            value={group.is_active ? 'Active' : 'Inactive'}
                            valueColor={group.is_active ? 'text-green-600' : 'text-secondary'}
                        />
                    </div>

                    {/* Hierarchy Path */}
                    {group.hierarchy_path && (
                        <div className="pt-4 mt-4 border-t border-default">
                            <p className="text-xs text-muted mb-2">Hierarchy Path</p>
                            <div className="flex flex-wrap items-center gap-2">
                                {group.hierarchy_path.split('/').map((part, index, arr) => (
                                    <React.Fragment key={index}>
                                        <span className={`px-2 py-1 rounded text-xs font-medium ${
                                            index === arr.length - 1
                                                ? 'bg-primary-50 text-primary-700'
                                                : 'bg-surface-raised text-secondary'
                                        }`}>
                                            {part.trim()}
                                        </span>
                                        {index < arr.length - 1 && (
                                            <ChevronRight className="w-3 h-3 text-muted" />
                                        )}
                                    </React.Fragment>
                                ))}
                            </div>
                        </div>
                    )}
                </div>

                {/* Inheritance Summary */}
                <div className="bg-surface border border-default rounded-lg p-6">
                    <h3 className="text-sm font-semibold text-default mb-4 flex items-center space-x-2">
                        <TrendingUp className="w-4 h-4 text-secondary" />
                        <span>Resource Inheritance</span>
                    </h3>

                    <div className="grid grid-cols-2 gap-3">
                        <div className="bg-surface-alt rounded-lg p-3 text-center">
                            <p className="text-xs text-muted mb-1">Total Resources</p>
                            <p className="text-xl font-bold text-default">
                                {(stats?.member_count_with_descendants || 0) +
                                    (stats?.llm_model_count_with_inherited || 0) +
                                    (stats?.server_config_count_with_inherited || 0) +
                                    (stats?.access_key_count || 0)}
                            </p>
                        </div>
                        <div className="bg-surface-alt rounded-lg p-3 text-center">
                            <p className="text-xs text-muted mb-1">Direct</p>
                            <p className="text-xl font-bold text-primary">
                                {(stats?.member_count || 0) +
                                    (stats?.llm_model_count || 0) +
                                    (stats?.server_config_count || 0) +
                                    (stats?.access_key_count || 0)}
                            </p>
                        </div>
                        <div className="bg-surface-alt rounded-lg p-3 text-center">
                            <p className="text-xs text-muted mb-1">Inherited</p>
                            <p className="text-xl font-bold text-muted">
                                {((stats?.member_count_with_descendants || 0) - (stats?.member_count || 0)) +
                                    ((stats?.llm_model_count_with_inherited || 0) - (stats?.llm_model_count || 0)) +
                                    ((stats?.server_config_count_with_inherited || 0) - (stats?.server_config_count || 0))}
                            </p>
                        </div>
                        <div className="bg-surface-alt rounded-lg p-3 text-center">
                            <p className="text-xs text-muted mb-1">Inherit Enabled</p>
                            <p className={`text-xl font-bold ${group.inherit_resources ? 'text-green-600' : 'text-muted'}`}>
                                {group.inherit_resources ? 'Yes' : 'No'}
                            </p>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
};

// ============================================================================
// Sub-components
// ============================================================================

/** Small summary stat card */
const SummaryCard: React.FC<{
    icon: React.ReactNode;
    label: string;
    value: string;
    iconBg: string;
    iconColor: string;
}> = ({ icon, label, value, iconBg, iconColor }) => (
    <div className="bg-surface border border-default rounded-lg p-4">
        <div className="flex items-center gap-2 mb-2">
            <div className={`p-1.5 rounded-md ${iconBg} ${iconColor}`}>{icon}</div>
            <span className="text-xs text-muted">{label}</span>
        </div>
        <p className="text-xl font-bold text-default">{value}</p>
    </div>
);

/** Small metric box inside detail panels */
const MetricBox: React.FC<{
    label: string;
    value: string;
    sub?: string;
    highlight?: boolean;
}> = ({ label, value, sub, highlight }) => (
    <div className={`rounded-lg p-3 text-center ${highlight ? 'bg-surface-raised' : 'bg-surface-alt'}`}>
        <p className="text-xs text-muted mb-1">{label}</p>
        <p className={`text-sm font-bold ${highlight ? 'text-default' : 'text-secondary'}`}>{value}</p>
        {sub && <p className="text-xs text-muted mt-0.5">{sub}</p>}
    </div>
);

/** Info row for hierarchy section */
const InfoRow: React.FC<{
    icon: React.ReactNode;
    label: string;
    value: string;
    valueColor?: string;
}> = ({ icon, label, value, valueColor = 'text-default' }) => (
    <div className="flex items-center justify-between p-3 bg-surface-alt rounded-lg">
        <div className="flex items-center space-x-3">
            <div className="p-2 bg-surface rounded-lg">{icon}</div>
            <div>
                <p className="text-xs text-muted">{label}</p>
                <p className={`text-sm font-semibold ${valueColor}`}>{value}</p>
            </div>
        </div>
    </div>
);

export default GroupUsageTab;
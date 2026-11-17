/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Mission Control Chart Components
 * Reusable chart/visualization components for the unified monitoring dashboard
 *
 * Includes the metrics charts plus:
 *  - ProxyTypeBreakdownChart
 *  - TokenUsageSummary
 */

import React from 'react';
import {
    LineChart, Line, AreaChart, Area, BarChart, Bar,
    PieChart, Pie, Cell, XAxis, YAxis, CartesianGrid,
    Tooltip, Legend, ResponsiveContainer, PieLabelRenderProps
} from 'recharts';
import {
    Brain, DollarSign, Hash
} from 'lucide-react';
import { bucketRequestsBySurface } from '../../lib/proxyTypeBuckets';
import { TABLE_TH_TYPE, TABLE_THEAD, TABLE_TBODY } from '../ui/tableStyles';
import { bucketDaily } from '../../lib/timeBuckets';


// ============================================================================
// Shared types and constants
// ============================================================================

interface TimeSeriesData {
    time: string;
    requests?: number;
    totalRequests?: number;
    successfulRequests?: number;
    failedRequests?: number;
    successRate?: number;
    avgDuration?: number;
    p50?: number;
    p95?: number;
    p99?: number;
    errorRate?: number;
    errorCount?: number;
}

interface MCPMethodData {
    count: number;
    avg_duration: number;
    success_rate: number;
    server_name?: string;
}

const COLORS = {
    primary: '#3b82f6',
    success: '#10b981',
    warning: '#f59e0b',
    error: '#ef4444',
    purple: '#8b5cf6',
    indigo: '#6366f1',
    gray: '#6b7280',
    teal: '#14b8a6',
    pink: '#ec4899',
    errorPalette: ['#ef4444', '#f59e0b', '#f97316', '#ec4899', '#8b5cf6', '#6366f1'],
    proxyPalette: ['#3b82f6', '#8b5cf6', '#f59e0b', '#14b8a6'],
};

// Recharts has THREE separate tooltip style slots —
//   contentStyle → outer wrapper
//   labelStyle   → top header (the X-axis label / category name)
//   itemStyle    → each data row (label + value)
// Only styling the container leaves item rows on Recharts'
// default near-black colour, which on dark mode renders as
// "washed-out grey on grey". We name all three and reuse them
// across every <Tooltip/> in this file. Using the bg-surface-
// alt token rather than bg-surface so the tooltip pops a step
// brighter than whatever card/chart sits behind it.
const tooltipStyle = {
    backgroundColor: 'var(--color-surface-alt)',
    border: '1px solid var(--color-border)',
    borderRadius: '8px',
    fontSize: '12px',
    color: 'var(--color-text)',
    boxShadow: '0 4px 12px rgba(0, 0, 0, 0.25)',
};

const tooltipItemStyle = {
    color: 'var(--color-text)',
};

const tooltipLabelStyle = {
    color: 'var(--color-text-secondary)',
    fontWeight: 600,
    marginBottom: '4px',
};

const makeTimeFormatter = (data: Array<{ time?: string }>) => {
    // Determine span of data to pick the right label format
    let spanHours = 24; // fallback
    if (data.length >= 2) {
        const first = new Date(data[0].time || '').getTime();
        const last = new Date(data[data.length - 1].time || '').getTime();
        if (!isNaN(first) && !isNaN(last)) {
            spanHours = (last - first) / (1000 * 60 * 60);
        }
    }
    return (value: string) => {
        try {
            const d = new Date(value);
            if (spanHours <= 6) {
                // Short window: just HH:MM
                return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            } else if (spanHours <= 36) {
                // ~24h window: abbreviated day + time so you can see date transitions
                const day = d.toLocaleDateString([], { weekday: 'short' });
                const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                return `${day} ${time}`;
            } else {
                // Multi-day (7d+): date + time
                const day = d.toLocaleDateString([], { month: 'short', day: 'numeric' });
                const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                return `${day} ${time}`;
            }
        } catch {
            return value;
        }
    };
};


// ============================================================================
// REQUEST VOLUME CHART
// ============================================================================

export const RequestVolumeChart: React.FC<{ data: TimeSeriesData[] }> = React.memo(({ data }) => {
    if (!data || data.length === 0) {
        return (
            <div className="bg-surface p-6 rounded-lg border">
                <h4 className="font-medium text-default mb-4">Request Volume Over Time</h4>
                <div className="h-[250px] flex items-center justify-center">
                    <p className="text-sm text-muted">No request data available</p>
                </div>
            </div>
        );
    }

    // Normalize — backend sends either `requests` or `totalRequests`
    const normalized = data.map(d => ({
        ...d,
        requests: d.requests ?? d.totalRequests ?? 0,
        failed: d.failedRequests ?? 0,
    }));

    const fmtTick = makeTimeFormatter(data);

    return (
        <div className="bg-surface p-5 rounded-lg border">
            <h4 className="text-sm font-semibold text-default mb-3">Request Volume Over Time</h4>
            <ResponsiveContainer width="100%" height={250}>
                <AreaChart data={normalized}>
                    <defs>
                        <linearGradient id="gradRequests" x1="0" y1="0" x2="0" y2="1">
                            <stop offset="5%" stopColor={COLORS.primary} stopOpacity={0.3} />
                            <stop offset="95%" stopColor={COLORS.primary} stopOpacity={0.02} />
                        </linearGradient>
                        <linearGradient id="gradFailed" x1="0" y1="0" x2="0" y2="1">
                            <stop offset="5%" stopColor={COLORS.error} stopOpacity={0.2} />
                            <stop offset="95%" stopColor={COLORS.error} stopOpacity={0.02} />
                        </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
                    <XAxis dataKey="time" tick={{ fontSize: 11 }} tickFormatter={fmtTick} />
                    <YAxis tick={{ fontSize: 11 }} />
                    <Tooltip
                        contentStyle={tooltipStyle}
                        itemStyle={tooltipItemStyle}
                        labelStyle={tooltipLabelStyle}
                        labelFormatter={v => new Date(v).toLocaleString()}
                    />
                    <Legend wrapperStyle={{ fontSize: 11 }} />
                    <Area
                        type="monotone"
                        dataKey="requests"
                        stroke={COLORS.primary}
                        strokeWidth={2}
                        fillOpacity={1}
                        fill="url(#gradRequests)"
                        name="Total Requests"
                    />
                    <Area
                        type="monotone"
                        dataKey="failed"
                        stroke={COLORS.error}
                        strokeWidth={1.5}
                        fillOpacity={1}
                        fill="url(#gradFailed)"
                        name="Failed"
                    />
                </AreaChart>
            </ResponsiveContainer>
        </div>
    );
});
RequestVolumeChart.displayName = 'RequestVolumeChart';


// ============================================================================
// LATENCY PERCENTILES CHART
// ============================================================================

export const LatencyPercentilesChart: React.FC<{ data: TimeSeriesData[] }> = React.memo(({ data }) => {
    if (!data || data.length === 0) {
        return (
            <div className="bg-surface p-6 rounded-lg border">
                <h4 className="font-medium text-default mb-4">Response Time Percentiles</h4>
                <div className="h-[250px] flex items-center justify-center">
                    <p className="text-sm text-muted">No latency data available</p>
                </div>
            </div>
        );
    }

    return (
        <div className="bg-surface p-5 rounded-lg border">
            <h4 className="text-sm font-semibold text-default mb-3">Response Time Percentiles</h4>
            <ResponsiveContainer width="100%" height={250}>
                <LineChart data={data}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
                    <XAxis dataKey="time" tick={{ fontSize: 11 }} tickFormatter={makeTimeFormatter(data)} />
                    <YAxis
                        tick={{ fontSize: 11 }}
                        label={{ value: 'ms', angle: -90, position: 'insideLeft', style: { fontSize: 10 } }}
                    />
                    <Tooltip
                        contentStyle={tooltipStyle}
                        itemStyle={tooltipItemStyle}
                        labelStyle={tooltipLabelStyle}
                        labelFormatter={v => new Date(v).toLocaleString()}
                        formatter={(value: number) => [value ? `${value.toFixed(0)}ms` : 'N/A', '']}
                    />
                    <Legend wrapperStyle={{ fontSize: 11 }} />
                    <Line type="monotone" dataKey="p50" stroke={COLORS.success} strokeWidth={2} dot={false} name="P50 (Median)" />
                    <Line type="monotone" dataKey="p95" stroke={COLORS.warning} strokeWidth={2} dot={false} name="P95" />
                    <Line type="monotone" dataKey="p99" stroke={COLORS.error} strokeWidth={2} dot={false} name="P99" />
                </LineChart>
            </ResponsiveContainer>
        </div>
    );
});
LatencyPercentilesChart.displayName = 'LatencyPercentilesChart';


// ============================================================================
// ERROR RATE CHART
// ============================================================================

export const ErrorRateChart: React.FC<{ data: TimeSeriesData[] }> = React.memo(({ data }) => {
    if (!data || data.length === 0) {
        return (
            <div className="bg-surface p-6 rounded-lg border">
                <h4 className="font-medium text-default mb-4">Error Rate Over Time</h4>
                <div className="h-[200px] flex items-center justify-center">
                    <p className="text-sm text-muted">No error data available</p>
                </div>
            </div>
        );
    }

    return (
        <div className="bg-surface p-5 rounded-lg border">
            <h4 className="text-sm font-semibold text-default mb-3">Error Rate Over Time</h4>
            <ResponsiveContainer width="100%" height={200}>
                <AreaChart data={data}>
                    <defs>
                        <linearGradient id="gradError" x1="0" y1="0" x2="0" y2="1">
                            <stop offset="5%" stopColor={COLORS.error} stopOpacity={0.3} />
                            <stop offset="95%" stopColor={COLORS.error} stopOpacity={0.02} />
                        </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
                    <XAxis dataKey="time" tick={{ fontSize: 11 }} tickFormatter={makeTimeFormatter(data)} />
                    <YAxis tick={{ fontSize: 11 }} unit="%" />
                    <Tooltip
                        contentStyle={tooltipStyle}
                        itemStyle={tooltipItemStyle}
                        labelStyle={tooltipLabelStyle}
                        labelFormatter={v => new Date(v).toLocaleString()}
                        formatter={(value: number) => [`${value.toFixed(2)}%`, 'Error Rate']}
                    />
                    <Area
                        type="monotone"
                        dataKey="errorRate"
                        stroke={COLORS.error}
                        strokeWidth={2}
                        fillOpacity={1}
                        fill="url(#gradError)"
                    />
                </AreaChart>
            </ResponsiveContainer>
        </div>
    );
});
ErrorRateChart.displayName = 'ErrorRateChart';


// ============================================================================
// SUCCESS RATE GAUGE
// ============================================================================

export const SuccessRateGauge: React.FC<{ rate: number }> = React.memo(({ rate }) => {
    const color = rate >= 99 ? COLORS.success : rate >= 95 ? COLORS.warning : COLORS.error;
    const label = rate >= 99 ? 'Excellent' : rate >= 95 ? 'Good' : rate >= 90 ? 'Degraded' : 'Critical';

    return (
        <div className="bg-surface p-5 rounded-lg border">
            <h4 className="text-sm font-semibold text-default mb-3">Success Rate</h4>
            <div className="flex items-center justify-center h-[170px]">
                <div className="text-center">
                    <div className="relative w-28 h-28 mx-auto">
                        <svg viewBox="0 0 100 100" className="w-full h-full -rotate-90">
                            <circle cx="50" cy="50" r="42" fill="none" stroke="#f3f4f6" strokeWidth="8" />
                            <circle
                                cx="50" cy="50" r="42" fill="none"
                                stroke={color}
                                strokeWidth="8"
                                strokeLinecap="round"
                                strokeDasharray={`${rate * 2.64} 264`}
                            />
                        </svg>
                        <div className="absolute inset-0 flex items-center justify-center">
                            <span className="text-xl font-bold" style={{ color }}>{rate.toFixed(1)}%</span>
                        </div>
                    </div>
                    <p className="text-sm font-medium mt-2" style={{ color }}>{label}</p>
                </div>
            </div>
        </div>
    );
});
SuccessRateGauge.displayName = 'SuccessRateGauge';


// ============================================================================
// ERROR DISTRIBUTION PIE CHART
// ============================================================================

export const ErrorDistributionChart: React.FC<{ data: Array<[string, number]> }> = React.memo(({ data }) => {
    if (!data || data.length === 0) return null;

    const chartData = data.slice(0, 6).map(([name, value]) => ({
        name: name.length > 30 ? name.substring(0, 30) + '...' : name,
        fullName: name,
        value,
    }));

    const renderLabel = (props: PieLabelRenderProps) => {
        const { name } = props;
        const pct = Number(props.percent) || 0;
        if (pct < 0.05) return null;
        return <text x={props.x} y={props.y} textAnchor={props.textAnchor} fill="#374151" fontSize={10}>
            {name} ({(pct * 100).toFixed(0)}%)
        </text>;
    };

    return (
        <div className="bg-surface p-5 rounded-lg border">
            <h4 className="text-sm font-semibold text-default mb-3">Error Distribution</h4>
            <ResponsiveContainer width="100%" height={220}>
                <PieChart>
                    <Pie
                        data={chartData}
                        cx="50%"
                        cy="50%"
                        outerRadius={80}
                        innerRadius={40}
                        dataKey="value"
                        label={renderLabel}
                        labelLine={{ strokeWidth: 1 }}
                    >
                        {chartData.map((_, index) => (
                            <Cell key={`cell-${index}`} fill={COLORS.errorPalette[index % COLORS.errorPalette.length]} />
                        ))}
                    </Pie>
                    <Tooltip
                        contentStyle={tooltipStyle}
                        itemStyle={tooltipItemStyle}
                        labelStyle={tooltipLabelStyle}
                        formatter={(value: number, name: string) => [value.toLocaleString(), name]}
                    />
                </PieChart>
            </ResponsiveContainer>
        </div>
    );
});
ErrorDistributionChart.displayName = 'ErrorDistributionChart';


// ============================================================================
// MCP METHODS CHART
// ============================================================================

// Reusable table for MCP method/tool data and A2A capabilities. The
// `serverName` column doubles as "agent card name" for A2A — both
// surfaces have a parent entity per row, just labelled differently.
export const MCPMethodTable: React.FC<{
    items: Array<{ name: string; fullName: string; count: number; avgDuration: number; successRate: number; serverName?: string }>;
    showServer?: boolean;
}> = ({ items, showServer }) => (
    <div className="max-h-48 overflow-y-auto">
        <table className="w-full text-xs">
            <thead className={`${TABLE_THEAD} sticky top-0`}>
            <tr>
                <th className={`text-left p-1.5 ${TABLE_TH_TYPE}`}>Name</th>
                <th className={`text-right p-1.5 ${TABLE_TH_TYPE}`}>Calls</th>
                <th className={`text-right p-1.5 ${TABLE_TH_TYPE}`}>Avg</th>
                <th className={`text-right p-1.5 ${TABLE_TH_TYPE}`}>Success</th>
            </tr>
            </thead>
            <tbody className={TABLE_TBODY}>
            {items.map(item => (
                <tr key={item.fullName} className="hover:bg-surface-alt">
                    <td className="p-1.5 text-secondary" title={item.fullName}>
                        {item.name}
                        {showServer && item.serverName && (
                            <span className="text-muted ml-1">({item.serverName})</span>
                        )}
                    </td>
                    <td className="p-1.5 text-right text-secondary">{item.count.toLocaleString()}</td>
                    <td className="p-1.5 text-right text-secondary">{item.avgDuration.toFixed(0)}ms</td>
                    <td className={`p-1.5 text-right font-medium ${
                        item.successRate >= 99 ? 'text-green-600' :
                            item.successRate >= 95 ? 'text-yellow-600' :
                                'text-red-600'
                    }`}>
                        {item.successRate.toFixed(1)}%
                    </td>
                </tr>
            ))}
            </tbody>
        </table>
    </div>
);

export const MCPMethodsChart: React.FC<{
    data: Record<string, MCPMethodData>;
    toolCalls?: Record<string, MCPMethodData>;
}> = React.memo(({ data, toolCalls }) => {
    const hasSystemMethods = data && Object.keys(data).length > 0;
    const hasToolCalls = toolCalls && Object.keys(toolCalls).length > 0;

    if (!hasSystemMethods && !hasToolCalls) return null;

    const formatItems = (source: Record<string, MCPMethodData>, limit = 8) =>
        Object.entries(source)
            .sort(([, a], [, b]) => b.count - a.count)
            .slice(0, limit)
            .map(([name, metrics]) => ({
                name: name,
                fullName: name,
                count: metrics.count,
                avgDuration: metrics.avg_duration,
                successRate: metrics.success_rate,
                serverName: metrics.server_name,
            }));

    const systemItems = hasSystemMethods ? formatItems(data) : [];
    const toolItems = hasToolCalls ? formatItems(toolCalls) : [];

    return (
        <div className="bg-surface p-5 rounded-lg border">
            {/* Tool Calls section — most interesting, show first */}
            {hasToolCalls && (
                <div className={hasSystemMethods ? 'mb-4' : ''}>
                    <h4 className="text-sm font-semibold text-default mb-2">Tool Calls</h4>
                    <MCPMethodTable items={toolItems} showServer />
                </div>
            )}

            {/* System Methods section */}
            {hasSystemMethods && (
                <div>
                    <h4 className="text-sm font-semibold text-default mb-2">System Methods</h4>
                    <MCPMethodTable items={systemItems} />
                </div>
            )}
        </div>
    );
});
MCPMethodsChart.displayName = 'MCPMethodsChart';


// ============================================================================
// A2A CAPABILITIES CHART
// ============================================================================
// Mirrors MCPMethodsChart for Agent-to-Agent traffic. The data
// contract is identical (count / avg_duration / success_rate per
// capability) so we can reuse MCPMethodTable. The only surface
// difference is the `card_name` label vs `server_name`: a capability
// is hosted by an agent card, not an MCP server.

export interface A2ACapabilityData {
    count: number;
    avg_duration: number;
    success_rate: number;
    card_name?: string | null;
}

export const A2ACapabilitiesChart: React.FC<{
    data: Record<string, A2ACapabilityData>;
}> = React.memo(({ data }) => {
    const hasData = data && Object.keys(data).length > 0;
    if (!hasData) return null;

    const items = Object.entries(data)
        .sort(([, a], [, b]) => b.count - a.count)
        .slice(0, 8)
        .map(([name, m]) => ({
            name,
            fullName: name,
            count: m.count,
            avgDuration: m.avg_duration,
            successRate: m.success_rate,
            serverName: m.card_name || undefined,
        }));

    return (
        <div className="bg-surface p-5 rounded-lg border">
            <h4 className="text-sm font-semibold text-default mb-2">A2A Capabilities</h4>
            <MCPMethodTable items={items} showServer />
        </div>
    );
});
A2ACapabilitiesChart.displayName = 'A2ACapabilitiesChart';


// ============================================================================
// USAGE HEATMAP (Simple 24-hour)
// ============================================================================

export const UsageHeatmap: React.FC<{ data: Record<number, number> }> = React.memo(({ data }) => {
    if (!data || Object.keys(data).length === 0) return null;

    const maxValue = Math.max(...Object.values(data), 1);
    const hours = Array.from({ length: 24 }, (_, i) => i);

    const getIntensity = (value: number): string => {
        if (!value) return 'bg-surface-alt';
        const ratio = value / maxValue;
        if (ratio > 0.8) return 'bg-blue-600';
        if (ratio > 0.6) return 'bg-blue-500';
        if (ratio > 0.4) return 'bg-blue-400';
        if (ratio > 0.2) return 'bg-blue-300';
        return 'bg-blue-200';
    };

    return (
        <div className="bg-surface p-5 rounded-lg border">
            <h4 className="text-sm font-semibold text-default mb-3">Request Volume by Hour</h4>
            <div className="space-y-1.5">
                <div className="grid grid-cols-24 gap-0.5">
                    {hours.map(hour => (
                        <div
                            key={hour}
                            className={`h-10 ${getIntensity(data[hour] || 0)} rounded-sm transition-all hover:ring-1 hover:ring-blue-500 cursor-pointer`}
                            title={`${hour}:00 — ${(data[hour] || 0).toLocaleString()} requests`}
                        />
                    ))}
                </div>
                <div className="grid grid-cols-24 gap-0.5 text-xs text-muted text-center">
                    {hours.map(hour => (
                        <div key={hour}>{hour % 6 === 0 ? hour : ''}</div>
                    ))}
                </div>
            </div>
        </div>
    );
});
UsageHeatmap.displayName = 'UsageHeatmap';


// ============================================================================
// PROXY TYPE BREAKDOWN CHART
// ============================================================================

export const ProxyTypeBreakdownChart: React.FC<{
    mcpMethods: Record<string, MCPMethodData> | null;
    a2aOverview?: { total_requests?: number } | null;
    performance: any;
    recentActivity?: any;
}> = React.memo(({ a2aOverview, performance }) => {
    // Build breakdown from `performance.requests_by_proxy_type` —
    // the per-`proxy_type` count the Rust gateway records on every
    // request. The shared `bucketRequestsBySurface` helper rolls
    // raw subtypes (`llm` / `batch` / `skill` / `mcp` / `portal` /
    // `oauth` / …) into the four display surfaces, so LLM and
    // Portal do not land in an "Other" bucket, which would make
    // the gap to per-group totals (which only count billable
    // surfaces) confusing to reconcile.
    const surf = bucketRequestsBySurface(performance?.requests_by_proxy_type);
    const breakdown: Array<{ name: string; value: number; color: string }> = [];

    if (surf.llm > 0) {
        breakdown.push({ name: 'LLM', value: surf.llm, color: COLORS.indigo });
    }
    if (surf.mcp > 0) {
        breakdown.push({ name: 'MCP', value: surf.mcp, color: COLORS.primary });
    }
    // A2A is sourced separately — the routes write only to
    // proxy_logs, not proxy_metrics, so it never lands in
    // requests_by_proxy_type. The a2a_overview aggregation
    // covers it. Same green that's used in the A2A KPI card and
    // capability breakdown elsewhere on the page.
    const a2aTotal = a2aOverview?.total_requests || 0;
    if (a2aTotal > 0) {
        breakdown.push({ name: 'A2A', value: a2aTotal, color: COLORS.success });
    }
    if (surf.portal > 0) {
        breakdown.push({ name: 'Portal', value: surf.portal, color: COLORS.warning });
    }
    if (surf.other > 0) {
        breakdown.push({ name: 'Other', value: surf.other, color: COLORS.gray });
    }

    if (breakdown.length === 0) {
        return (
            <div className="bg-surface p-5 rounded-lg border">
                <h4 className="text-sm font-semibold text-default mb-3">Requests by Surface</h4>
                <div className="h-[200px] flex items-center justify-center">
                    <p className="text-sm text-muted">No breakdown data</p>
                </div>
            </div>
        );
    }

    const total = breakdown.reduce((s, b) => s + b.value, 0);

    return (
        <div className="bg-surface p-5 rounded-lg border">
            <h4 className="text-sm font-semibold text-default mb-3">Requests by Surface</h4>
            <ResponsiveContainer width="100%" height={200}>
                <PieChart>
                    <Pie
                        data={breakdown}
                        cx="50%"
                        cy="50%"
                        outerRadius={70}
                        innerRadius={35}
                        dataKey="value"
                        label={({ name, percent }: any) => {
                            const pct = Number(percent) || 0;
                            return pct > 0.05 ? `${name} ${(pct * 100).toFixed(0)}%` : '';
                        }}
                        labelLine={{ strokeWidth: 1 }}
                    >
                        {breakdown.map((entry, index) => (
                            <Cell key={`cell-${index}`} fill={entry.color} />
                        ))}
                    </Pie>
                    <Tooltip
                        contentStyle={tooltipStyle}
                        itemStyle={tooltipItemStyle}
                        labelStyle={tooltipLabelStyle}
                        formatter={(value: number, name: string) => [
                            `${value.toLocaleString()} (${total > 0 ? ((value / total) * 100).toFixed(1) : 0}%)`,
                            name,
                        ]}
                    />
                </PieChart>
            </ResponsiveContainer>
            {/* Legend strip */}
            <div className="flex flex-wrap justify-center gap-3 mt-2">
                {breakdown.map(b => (
                    <div key={b.name} className="flex items-center gap-1.5 text-xs">
                        <div className="w-2.5 h-2.5 rounded-sm" style={{ backgroundColor: b.color }} />
                        <span className="text-secondary">{b.name}</span>
                        <span className="text-muted">{b.value.toLocaleString()}</span>
                    </div>
                ))}
            </div>
        </div>
    );
});
ProxyTypeBreakdownChart.displayName = 'ProxyTypeBreakdownChart';


// ============================================================================
// Monthly LLM economics series (the "LLM Economics" tab)
// ============================================================================

export interface MonthlyCostPoint {
    month: string;                 // "2026-07"
    cost_usd: number;
    tokens: number;
    requests: number;
    unit_cost_per_1k_usd: number;
}

/** "Jan '26" from "2026-01" — compact month ticks for a 12-month axis. */
const fmtMonth = (m: string): string => {
    const [y, mm] = m.split('-');
    const names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                   'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const name = names[parseInt(mm, 10) - 1] ?? m;
    return mm === '01' ? `${name} '${y.slice(2)}` : name;
};

/**
 * Unit cost per 1,000 LLM requests, by calendar month. The FinOps
 * reduction story lives here: smart routing and caching show up as a
 * falling line even while volume grows — a period total can't show that.
 */
export const MonthlyUnitCostChart: React.FC<{ data: MonthlyCostPoint[] }> =
    React.memo(({ data }) => {
    if (!data || data.length === 0) {
        return (
            <div className="bg-surface p-5 rounded-lg border">
                <h4 className="text-sm font-semibold text-default mb-3">LLM Cost per 1,000 Requests</h4>
                <div className="h-[220px] flex items-center justify-center">
                    <p className="text-sm text-muted">No monthly LLM cost data yet</p>
                </div>
            </div>
        );
    }
    return (
        <div className="bg-surface p-5 rounded-lg border">
            <h4 className="text-sm font-semibold text-default mb-3">LLM Cost per 1,000 Requests</h4>
            <ResponsiveContainer width="100%" height={220}>
                <LineChart data={data}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
                    <XAxis dataKey="month" tick={{ fontSize: 11 }} tickFormatter={fmtMonth} />
                    <YAxis tick={{ fontSize: 11 }} tickFormatter={v => `$${v}`} />
                    <Tooltip
                        contentStyle={tooltipStyle}
                        itemStyle={tooltipItemStyle}
                        labelStyle={tooltipLabelStyle}
                        formatter={(value: number) => [`$${value.toFixed(2)}`, 'Cost / 1k req']}
                        labelFormatter={(m: string) => fmtMonth(m)}
                    />
                    <Line
                        type="monotone"
                        dataKey="unit_cost_per_1k_usd"
                        stroke={COLORS.teal}
                        strokeWidth={2}
                        dot={{ r: 3 }}
                        activeDot={{ r: 5 }}
                        isAnimationActive={false}
                    />
                </LineChart>
            </ResponsiveContainer>
        </div>
    );
});
MonthlyUnitCostChart.displayName = 'MonthlyUnitCostChart';

/** LLM request volume by calendar month — the denominator of the unit-cost line. */
export const MonthlyRequestsChart: React.FC<{ data: MonthlyCostPoint[] }> =
    React.memo(({ data }) => {
    if (!data || data.length === 0) {
        return (
            <div className="bg-surface p-5 rounded-lg border">
                <h4 className="text-sm font-semibold text-default mb-3">LLM Requests per Month</h4>
                <div className="h-[220px] flex items-center justify-center">
                    <p className="text-sm text-muted">No monthly LLM request data yet</p>
                </div>
            </div>
        );
    }
    return (
        <div className="bg-surface p-5 rounded-lg border">
            <h4 className="text-sm font-semibold text-default mb-3">LLM Requests per Month</h4>
            <ResponsiveContainer width="100%" height={220}>
                <BarChart data={data}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
                    <XAxis dataKey="month" tick={{ fontSize: 11 }} tickFormatter={fmtMonth} />
                    <YAxis tick={{ fontSize: 11 }} tickFormatter={v => v >= 1000 ? `${(v / 1000).toFixed(0)}k` : `${v}`} />
                    <Tooltip
                        contentStyle={tooltipStyle}
                        itemStyle={tooltipItemStyle}
                        labelStyle={tooltipLabelStyle}
                        formatter={(value: number) => [value.toLocaleString(), 'Requests']}
                        labelFormatter={(m: string) => fmtMonth(m)}
                    />
                    <Bar dataKey="requests" fill={COLORS.primary} radius={[4, 4, 0, 0]} isAnimationActive={false} />
                </BarChart>
            </ResponsiveContainer>
        </div>
    );
});
MonthlyRequestsChart.displayName = 'MonthlyRequestsChart';


// ============================================================================
// TOKEN USAGE SUMMARY
// ============================================================================

export const TokenUsageSummary: React.FC<{
    mcpMethods: Record<string, MCPMethodData> | null;
    performance: any;
}> = React.memo(({ mcpMethods, performance }) => {
    // Currently token data comes from the llm_data JSON fields aggregated
    // by the backend. Display a summary card structure that will populate
    // as LLM proxy traffic flows through the gateway.

    const totalRequests = performance?.total_requests || 0;
    const mcpRequests = mcpMethods
        ? Object.values(mcpMethods).reduce((s, m) => s + m.count, 0)
        : 0;
    const estimatedLLMRequests = Math.max(0, totalRequests - mcpRequests);

    return (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {/* LLM Request Volume */}
            <div className="bg-surface rounded-lg border p-4">
                <div className="flex items-center gap-2 mb-3">
                    <div className="p-1.5 rounded-md bg-purple-50 text-purple-600">
                        <Brain className="w-4 h-4" />
                    </div>
                    <span className="text-xs text-muted">LLM Requests</span>
                </div>
                <p className="text-2xl font-bold text-default">{estimatedLLMRequests.toLocaleString()}</p>
                <p className="text-xs text-muted mt-1">
                    Estimated from total ({totalRequests.toLocaleString()}) minus MCP ({mcpRequests.toLocaleString()})
                </p>
            </div>

            {/* Token Usage placeholder */}
            <div className="bg-surface rounded-lg border p-4">
                <div className="flex items-center gap-2 mb-3">
                    <div className="p-1.5 rounded-md bg-blue-50 text-blue-600">
                        <Hash className="w-4 h-4" />
                    </div>
                    <span className="text-xs text-muted">Token Usage</span>
                </div>
                <p className="text-lg font-bold text-default">—</p>
                <p className="text-xs text-muted mt-1">
                    Token counts available when LLM proxy records prompt/completion tokens
                </p>
                <div className="mt-3 space-y-1.5">
                    <div className="flex items-center justify-between text-xs">
                        <span className="text-muted">Prompt tokens</span>
                        <span className="text-disabled">—</span>
                    </div>
                    <div className="flex items-center justify-between text-xs">
                        <span className="text-muted">Completion tokens</span>
                        <span className="text-disabled">—</span>
                    </div>
                </div>
            </div>

            {/* Cost Summary placeholder */}
            <div className="bg-surface rounded-lg border p-4">
                <div className="flex items-center gap-2 mb-3">
                    <div className="p-1.5 rounded-md bg-green-50 text-green-600">
                        <DollarSign className="w-4 h-4" />
                    </div>
                    <span className="text-xs text-muted">Estimated Cost</span>
                </div>
                <p className="text-lg font-bold text-default">—</p>
                <p className="text-xs text-muted mt-1">
                    Cost estimation available when LLM proxy records pricing data
                </p>
                <div className="mt-3 space-y-1.5">
                    <div className="flex items-center justify-between text-xs">
                        <span className="text-muted">This period</span>
                        <span className="text-disabled">—</span>
                    </div>
                    <div className="flex items-center justify-between text-xs">
                        <span className="text-muted">Avg per request</span>
                        <span className="text-disabled">—</span>
                    </div>
                </div>
            </div>
        </div>
    );
});
TokenUsageSummary.displayName = 'TokenUsageSummary';
// ── Cost composition over time ─────────────────────────────────

export interface CostSeriesResponse {
    dimension: string;
    grain: string;
    keys: Array<{ key: string; name: string }>;
    data: Array<{ date: string; values: Record<string, { cost: number; requests: number }> }>;
}

/** Fixed band palette — color follows the entity (its position in the
 *  window's top-N ordering), never re-derived per render. "Other" is
 *  always gray so the tail can't be mistaken for a named provider. */
const COMPOSITION_PALETTE = [
    COLORS.primary, COLORS.teal, COLORS.purple, COLORS.warning,
    COLORS.pink, COLORS.indigo,
];

/**
 * Stacked area of daily cost per dimension key, rolled up to the shared
 * day→week→month grain client-side (lib/timeBuckets — the same rule as
 * RG Usage). Bands sum to the real total because the backend folds the
 * tail into "other" rather than dropping it.
 */
export const CostCompositionChart: React.FC<{ series: CostSeriesResponse | null }> =
    React.memo(({ series }) => {
    const flat = React.useMemo(() => {
        if (!series || series.data.length === 0) return null;
        const keys = series.keys.map(k => k.key);
        const daily = series.data.map(d => {
            const row: Record<string, number | string> = { date: d.date };
            for (const k of keys) row[k] = d.values[k]?.cost ?? 0;
            return row as { date: string } & Record<string, number>;
        });
        const { grain, rows } = bucketDaily(daily);
        return { keys, grain, rows };
    }, [series]);

    if (!series || !flat) {
        return (
            <div className="bg-surface p-5 rounded-lg border">
                <h4 className="text-sm font-semibold text-default mb-3">Cost Composition Over Time</h4>
                <div className="h-[240px] flex items-center justify-center">
                    <p className="text-sm text-muted">No cost data for this period</p>
                </div>
            </div>
        );
    }

    const nameOf = new Map(series.keys.map(k => [k.key, k.name]));
    const fmtBucket = (d: string) =>
        flat.grain === 'Monthly'
            ? new Date(d + '-01T00:00:00Z').toLocaleDateString(undefined, { month: 'short', year: '2-digit' })
            : new Date(d + 'T00:00:00Z').toLocaleDateString(undefined, { month: 'short', day: 'numeric' });

    return (
        <div className="bg-surface p-5 rounded-lg border" data-testid="mc-cost-composition">
            <h4 className="text-sm font-semibold text-default mb-1">
                Cost Composition Over Time
                <span className="ml-2 text-xs font-normal text-muted">
                    by {series.dimension} · {flat.grain.toLowerCase()} buckets
                </span>
            </h4>
            <ResponsiveContainer width="100%" height={240}>
                <AreaChart data={flat.rows}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
                    <XAxis dataKey="date" tick={{ fontSize: 11 }} tickFormatter={fmtBucket} />
                    <YAxis tick={{ fontSize: 11 }} tickFormatter={v => `$${v}`} />
                    <Tooltip
                        contentStyle={tooltipStyle}
                        itemStyle={tooltipItemStyle}
                        labelStyle={tooltipLabelStyle}
                        formatter={(value: number, key: string) => [
                            `$${value.toFixed(2)}`, nameOf.get(key) ?? key,
                        ]}
                        labelFormatter={fmtBucket}
                    />
                    <Legend
                        formatter={(key: string) => nameOf.get(key) ?? key}
                        wrapperStyle={{ fontSize: 11 }}
                    />
                    {flat.keys.map((k, i) => (
                        <Area
                            key={k}
                            type="monotone"
                            dataKey={k}
                            stackId="cost"
                            stroke={k === 'other' ? COLORS.gray : COMPOSITION_PALETTE[i % COMPOSITION_PALETTE.length]}
                            fill={k === 'other' ? COLORS.gray : COMPOSITION_PALETTE[i % COMPOSITION_PALETTE.length]}
                            fillOpacity={0.55}
                            isAnimationActive={false}
                        />
                    ))}
                </AreaChart>
            </ResponsiveContainer>
        </div>
    );
});
CostCompositionChart.displayName = 'CostCompositionChart';

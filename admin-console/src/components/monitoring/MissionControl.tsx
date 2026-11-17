/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Mission Control — Unified AI Gateway Monitoring Dashboard
 *
 * Tabbed layout inspired by Datadog / New Relic / AWS CloudWatch:
 *  - Overview:       System health banner + KPI strip + alert summary
 *  - Traffic:        Request volume chart, proxy breakdown, error rate
 *  - Performance:    Latency percentiles, distribution, summary stats
 *  - Infrastructure: AI model + MCP server health, resource utilization
 *  - Operations:     LLM economics, MCP methods, top servers
 *  - Analytics:      Heatmap, error distribution, user types
 *  - Alerts:         Full usage, system & metrics alerts
 */

import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import {
    Activity,
    AlertTriangle,
    BarChart3,
    Bot,
    Brain,
    CheckCircle,
    Clock,
    Cpu,
    DollarSign,
    Download,
    HardDrive,
    Image as ImageIcon,
    MessageSquare,
    Mic,
    Speaker,
    Video,
    RefreshCw,
    Server,
    Share2,
    Shield,
    TrendingUp,
    Users,
    XCircle,
    Zap,
    Loader2,
} from 'lucide-react';
import MCPIcon from '../common/MCPIcon';
import { useGateway } from '../../hooks/useGateway';
import { useLocalStorageString } from '../../hooks/useLocalStorage';
import { useTimespanPreference, type Timespan } from '../../hooks/useTimespanPreference';
import { UsageAlertInfo } from '../../lib/groupTypes';
import type { GroupHierarchyTree } from '../../lib/groupTypes';
import { periodToHours, periodToRange } from '../../lib/periodRange';
import { downloadBlob, downloadCsv } from '../../lib/csvExport';
import { statementHtml } from '../../lib/statementHtml';
import { AssuranceBand, RunSignalsSection } from './MissionControlAssurance';
import type {
    AISystemFleetResponse,
    RunStatsResponse,
} from '../../lib/aiSystemTypes';
import { fmtCost } from './fmtCost';
import { mergeTabs, useSlot } from '../../app/slots';
import { TABLE_TH_TYPE, TABLE_THEAD, TABLE_TBODY } from '../ui/tableStyles';
import {
    RequestVolumeChart,
    LatencyPercentilesChart,
    ErrorRateChart,
    SuccessRateGauge,
    ErrorDistributionChart,
    MCPMethodsChart,
    A2ACapabilitiesChart,
    UsageHeatmap,
    ProxyTypeBreakdownChart,
    MonthlyUnitCostChart,
    MonthlyRequestsChart,
    CostCompositionChart,
    MonthlyCostPoint
} from './MissionControlCharts';
import { runBackground } from '../../lib/asyncSafe';
import { errorKind, failureSentence } from '../../lib/narrow';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';
import McpServerHealthRow from './McpServerHealthRow';
import { serverHealthReading, type ServerHealthStatus } from '../../lib/serverHealth';


// ============================================================================
// Types
// ============================================================================

interface ServerHealth {
    id: string;
    name: string;
    /** The server's health as the Control Plane states it; absent for a server never checked. */
    health?: ServerHealthStatus;
}

interface AIModelHealth {
    id: string;
    name: string;
    provider: string;
    status: 'healthy' | 'unhealthy' | 'degraded' | 'unknown';
    responseTime?: number;
    lastCheck?: string;
    error?: string;
}


interface TimeSeriesPoint {
    time: string;
    totalRequests?: number;
    successfulRequests?: number;
    failedRequests?: number;
    successRate?: number;
    avgDuration?: number;
    requests?: number;
    p50?: number;
    p95?: number;
    p99?: number;
    errorRate?: number;
    errorCount?: number;
}

interface RealTimeData {
    requests_per_minute: number;
    current_success_rate: number;
    active_sessions: number;
    active_servers: number;
    total_requests: number;
    failed_requests: number;
}

interface PerformanceData {
    total_requests: number;
    successful_requests: number;
    failed_requests: number;
    success_rate: number;
    avg_duration_ms: number;
    min_duration_ms: number;
    max_duration_ms: number;
    total_data_transferred_mb: number;
    avg_response_size_kb: number;
    response_time_percentiles: { p50: number; p75: number; p90: number; p95: number; p99: number };
    session_reuse_rate: number;
    cache_hit_rate: number;
    /** Per-`proxy_type` request count from the Rust gateway's
     *  `build_proxy_metric` writer. Keys are raw proxy_type values
     *  (`llm`, `mcp`, `oauth`, `portal`, `batch`, `skill`, …); the
     *  bucketing into LLM / MCP / Portal display groups happens in
     *  the UI so we can ship new subtypes without an API change. */
    requests_by_proxy_type?: Record<string, number>;
}

// `bucketRequestsBySurface` lives in lib/proxyTypeBuckets so the
// charts component can import it without round-tripping through
// this page module. See its docstring for the bucket definitions.

interface DashboardKPIs {
    requestsLastHour: number;
    requestsLastDay: number;
    activeConnections: number;
    errorRate: number;
    avgResponseTime: number;
    uptime: number;
    cpuUsage?: number;
    memoryUsage?: number;
}

interface LLMEconomics {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    estimated_cost_usd: number;
    llm_request_count: number;
    avg_cost_per_request: number;
    avg_tokens_per_request: number;
}

// Per-mode roll-up for the LLM Media panel. Each mode bills in its
// own unit (images, characters, seconds), so a token-based row would
// either skew analytics or hide the work entirely. The typed
// counters below mirror the four `media_*` columns on `proxy_log`.
interface MediaModeMetrics {
    request_count: number;
    cost_usd: number;
    images_generated: number;
    audio_chars: number;
    audio_seconds_input: number;
    video_seconds_output: number;
}

interface MediaUsage {
    request_count: number;
    estimated_cost_usd: number;
    modes: Partial<Record<
        'image_generation' | 'audio_speech' | 'audio_transcription' | 'video_generation',
        MediaModeMetrics
    >>;
}

interface MissionControlState {
    usageAlerts: UsageAlertInfo[];       // quota breaches — Governance reads these
    realTime: RealTimeData | null;
    recentActivity: {
        top_servers: Array<{ server_id: string; request_count: number }>;
        top_methods: Array<{ method: string; count: number }>;
        error_spike: boolean;
    } | null;
    performance: PerformanceData | null;
    errors: {
        error_rate: number;
        error_breakdown: Record<string, number>;
        top_errors: Array<[string, number]>;
    } | null;
    mcpMethods: Record<string, { count: number; avg_duration: number; success_rate: number }> | null;
    mcpToolCalls: Record<string, { count: number; avg_duration: number; success_rate: number; server_name?: string }> | null;
    topMcpServers: Array<{ server_id: string; server_name?: string; request_count: number }>;
    topLlmModels: Array<{ model_id: string; model_name: string; name: string; request_count: number }>;
    // A2A — Agent-to-Agent traffic. Sourced from proxy_logs (not
    // proxy_metrics) so the keys here are independent of the MCP
    // mcp_data shape but follow the same count/avg_duration/success_rate
    // contract for UI reuse.
    a2aCapabilities: Record<string, { count: number; avg_duration: number; success_rate: number; card_name?: string }> | null;
    topA2aAgents: Array<{ card_id: string; card_name: string; request_count: number }>;
    a2aOverview: {
        total_requests: number;
        successful_requests: number;
        success_rate: number;
        avg_duration_ms: number;
        total_data_transferred_mb: number;
    } | null;
    usagePatterns: {
        peak_hour: number | null;
        requests_by_hour: Record<number, number>;
        user_type_distribution: Record<string, number>;
    } | null;
    kpis: DashboardKPIs;
    serverHealth: ServerHealth[];
    aiModelHealth: AIModelHealth[];
    requestsTimeSeries: TimeSeriesPoint[];
    latencyTimeSeries: TimeSeriesPoint[];
    errorsTimeSeries: TimeSeriesPoint[];
    heatmapData: any;
    llmEconomics: LLMEconomics | null;
    mediaUsage: MediaUsage | null;
}

interface MissionControlProps {
    className?: string;
    setActiveTab?: (tab: string) => void;
    /** Open pre-scoped to one Resource Group (set by the card dashboard's
     *  per-card "Metrics" action via `mission-control-metrics:<id>`). */
    initialGroupId?: string;
}

type TabId = 'cost' | 'economics' | 'governance' | 'agents' | 'usage' | 'reliability';

/** A tab of the strip: one of this page's own or one contributed to its slot. */
interface TabDef {
    id: string;
    label: string;
    icon: React.ReactNode;
}


// ============================================================================
// Utility Functions
// ============================================================================

const fmt = (n: number | undefined): string => (n == null ? '0' : n.toLocaleString());

/** Compact display for token-scale numbers: 618,842,142 reads as
 *  "618.8M" on a stat tile. Below one million the full grouped number
 *  stays — 54,323 is still scannable, and over-compacting small counts
 *  loses precision operators actually use. Full value belongs in a
 *  title/tooltip at the call site when it matters. */
const fmtCompact = (n: number | undefined): string => {
    if (n == null) return '0';
    if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
    if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
    return n.toLocaleString();
};
const fmtDuration = (ms: number | undefined): string => {
    if (!ms) return '0ms';
    if (ms < 1000) return `${Math.round(ms)}ms`;
    return `${(ms / 1000).toFixed(2)}s`;
};
const fmtBytes = (mb: number | undefined): string => {
    if (!mb) return '0 B';
    if (mb < 1) return `${(mb * 1024).toFixed(0)} KB`;
    if (mb < 1024) return `${mb.toFixed(1)} MB`;
    return `${(mb / 1024).toFixed(2)} GB`;
};
const statusDot = (status: string) => {
    switch (status) {
        case 'healthy': return 'bg-green-500';
        case 'unhealthy': return 'bg-red-500';
        case 'degraded': return 'bg-yellow-500';
        default: return 'bg-gray-400';
    }
};

const statusIcon = (status: string) => {
    switch (status) {
        case 'healthy': return <CheckCircle className="w-4 h-4 text-green-600" />;
        case 'unhealthy': return <XCircle className="w-4 h-4 text-red-600" />;
        case 'degraded': return <AlertTriangle className="w-4 h-4 text-yellow-600" />;
        default: return <Clock className="w-4 h-4 text-muted" />;
    }
};

// Period options shown in the toolbar. Mirrors the resource-group usage view
// (24h / 7d / 30d / 90d / All Time). `all` maps to period_hours=0, which the
// metrics endpoints treat as "no lower bound".
const PERIOD_OPTIONS: { value: Timespan; label: string; title?: string }[] = [
    { value: '24h', label: '24 Hours' },
    { value: '7d', label: '7 Days' },
    { value: '30d', label: '30 Days' },
    { value: '90d', label: '90 Days' },
    // Calendar periods — FinOps reconciles against invoices, and invoices
    // are calendar months, not rolling windows.
    { value: 'mtd', label: 'MTD', title: 'Month to date (calendar)' },
    { value: 'lastm', label: 'Last Mo', title: 'Previous calendar month' },
    { value: 'all', label: 'All Time' },
];

/** Mission Control additionally supports an arbitrary calendar range,
 *  encoded into the same slot as `custom:<start>:<end>`. It cannot be
 *  enumerated, so it is matched by prefix instead. */
type MissionControlPeriod = Timespan | `custom:${string}:${string}`;

const isCustomRange = (raw: string) => raw.startsWith('custom:');

/** What the shared timespan preference is allowed to be on this surface.
 *  Derived from the options above so the two can't drift apart. */
const MISSION_CONTROL_TIMESPANS: readonly MissionControlPeriod[] =
    PERIOD_OPTIONS.map((o) => o.value);

// Provenance plane toggle — Brutor's two-plane story: enforce on the
// governed (inline) surface, observe across imported (desktop/web chat)
// telemetry. 'all' omits the filter.
const PLANE_OPTIONS: { value: string; label: string; title: string }[] = [
    { value: 'all', label: 'All', title: 'Governed inline + observed imported traffic' },
    { value: 'governed', label: 'Governed', title: 'Inline traffic the gateway proxied & enforced policy on' },
    { value: 'observed', label: 'Observed', title: 'Imported (Traffic Data Import) telemetry — observability only, never enforced' },
];

// Tabs whose data comes from usage_logs / proxy_logs (which carry the
// resource-group id) and can therefore be scoped to a single Resource Group
// (an AI System is an RG). The proxy_metrics-backed sections
// (usage/reliability) cannot. Overview is filterable:
// its fleet board / run-ledger sections are system-scoped, and the
// platform-wide infra KPIs below them are explicitly labelled as such when
// a system is selected.
const GROUP_FILTERABLE_TABS = new Set(['cost', 'economics', 'governance', 'agents']);

const intervalForPeriod = (period: string): number => {
    switch (period) {
        case '24h': return 60;     // hourly
        case '7d': return 360;     // 6-hourly
        case '30d': return 1440;   // daily
        case '90d': return 1440;   // daily
        case 'all': return 1440;   // daily (coarse — range can be large)
        default: break;
    }
    // Calendar ranges: pick from the range's actual length.
    const hours = periodToHours(period);
    if (hours <= 48) return 60;
    if (hours <= 14 * 24) return 360;
    return 1440;
};


// ============================================================================
// Sub-components
// ============================================================================

/** Resource utilization bar */
const UtilizationBar: React.FC<{
    label: string;
    value: number;
    icon: React.ReactNode;
    color: string;
}> = ({ label, value, icon, color }) => (
    <div>
        <div className="flex items-center justify-between mb-1.5">
            <div className="flex items-center gap-2">
                {icon}
                <span className="text-sm font-medium text-secondary">{label}</span>
            </div>
            <span className="text-sm font-bold text-default">{value.toFixed(1)}%</span>
        </div>
        <div className="w-full bg-surface-raised rounded-full h-2">
            <div
                className={`${color} h-2 rounded-full transition-all duration-500`}
                style={{ width: `${Math.min(100, value)}%` }}
            />
        </div>
    </div>
);


// ============================================================================
// Media Usage Panel
//
// Shown on the Operations tab below LLM Economics. The four media
// modes — image / TTS / STT / video — bill in their own units
// (images, characters, seconds), so a token-based row would either
// skew analytics or hide the work entirely. We render a small grid
// of per-mode tiles with the typed counter alongside the
// attributable cost. Unused modes are skipped — the empty grid
// state shows when no media traffic was recorded for the period.
// ============================================================================

const fmtSeconds = (s: number): string => {
    if (s >= 3600) return `${(s / 3600).toFixed(1)}h`;
    if (s >= 60) return `${(s / 60).toFixed(1)}m`;
    return `${s.toFixed(1)}s`;
};

const MEDIA_MODE_DEFS: Array<{
    key: 'image_generation' | 'audio_speech' | 'audio_transcription' | 'video_generation';
    label: string;
    unit: string;
    icon: React.ComponentType<{ className?: string }>;
    counterKey: keyof MediaModeMetrics;
    formatCounter: (n: number) => string;
}> = [
    {
        key: 'image_generation',
        label: 'Images',
        unit: 'images',
        icon: ImageIcon,
        counterKey: 'images_generated',
        formatCounter: (n) => n.toLocaleString(),
    },
    {
        key: 'audio_speech',
        label: 'Speech (TTS)',
        unit: 'chars',
        icon: Speaker,
        counterKey: 'audio_chars',
        formatCounter: (n) => n.toLocaleString(),
    },
    {
        key: 'audio_transcription',
        label: 'Transcription',
        unit: 'audio',
        icon: Mic,
        counterKey: 'audio_seconds_input',
        formatCounter: fmtSeconds,
    },
    {
        key: 'video_generation',
        label: 'Video',
        unit: 'output',
        icon: Video,
        counterKey: 'video_seconds_output',
        formatCounter: fmtSeconds,
    },
];

const MediaUsagePanel: React.FC<{ data: MediaUsage | null }> = ({ data }) => {
    const hasData = !!data && data.request_count > 0;
    return (
        <div className="mb-6">
            <h3 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3 flex items-center gap-2">
                <ImageIcon className="w-4 h-4" /> LLM Media Usage
            </h3>
            {hasData && data ? (
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
                    {MEDIA_MODE_DEFS.map(({ key, label, unit, icon: Icon, counterKey, formatCounter }) => {
                        const mode = data.modes[key];
                        // Mode tile renders even when empty so the
                        // four-up grid stays balanced — operators
                        // configuring (say) only TTS still see image
                        // / video / STT slots so they know the
                        // panel covers all four modes.
                        const counter = mode ? Number(mode[counterKey] ?? 0) : 0;
                        const reqs = mode?.request_count ?? 0;
                        const cost = mode?.cost_usd ?? 0;
                        return (
                            <div key={key} className="bg-surface rounded-lg border p-4">
                                <div className="flex items-center gap-2 mb-1">
                                    <Icon className="w-4 h-4 text-muted" />
                                    <div className="text-xs text-muted">{label}</div>
                                </div>
                                <div className="text-lg font-bold text-default">
                                    {formatCounter(counter)}
                                </div>
                                <div className="text-xs text-muted mt-1">
                                    {fmt(reqs)} req · {unit}
                                </div>
                                {cost > 0 && (
                                    <div className="text-xs text-green-700 mt-1">
                                        {fmtCost(cost)}
                                    </div>
                                )}
                            </div>
                        );
                    })}
                </div>
            ) : (
                <div className="bg-surface rounded-lg border p-6 text-center">
                    <ImageIcon className="w-8 h-8 text-disabled mx-auto mb-2" />
                    <p className="text-sm text-muted">
                        No image / audio / video requests recorded for this period
                    </p>
                </div>
            )}
        </div>
    );
};


// ============================================================================
// 7x24 Heatmap Grid
// ============================================================================

const HeatmapGrid: React.FC<{ data: any }> = ({ data }) => {
    if (!data?.heatmap || data.heatmap.length === 0) return null;

    const maxVal = data.max_value || 1;
    const dayNames = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

    const intensity = (val: number): string => {
        if (!val) return 'bg-surface-alt';
        const r = val / maxVal;
        if (r > 0.8) return 'bg-blue-600 text-white';
        if (r > 0.6) return 'bg-blue-500 text-white';
        if (r > 0.4) return 'bg-blue-400 text-white';
        if (r > 0.2) return 'bg-blue-300';
        return 'bg-blue-200';
    };

    return (
        <div className="bg-surface rounded-lg border p-4">
            <h4 className="text-sm font-semibold text-default mb-3 flex items-center gap-2">
                <BarChart3 className="w-4 h-4 text-muted" />
                Request Volume Heatmap
            </h4>
            <div className="overflow-x-auto">
                <div className="min-w-[700px]">
                    <div className="flex mb-1">
                        <div className="w-10" />
                        {Array.from({ length: 24 }, (_, h) => (
                            <div key={h} className="flex-1 text-center text-xs text-muted min-h-0 min-w-0">
                                {h % 3 === 0 ? h : ''}
                            </div>
                        ))}
                    </div>
                    {data.heatmap.map((day: any) => (
                        <div key={day.day_of_week} className="flex mb-0.5">
                            <div className="w-10 text-xs text-muted flex items-center">
                                {dayNames[day.day_of_week]}
                            </div>
                            {day.hourly_data.map((cell: any) => (
                                <div
                                    key={cell.hour}
                                    className={`flex-1 h-6 mx-px rounded-sm min-h-0 min-w-0 ${intensity(cell.value)} cursor-pointer transition-all hover:ring-1 hover:ring-blue-600`}
                                    title={`${dayNames[day.day_of_week]} ${cell.hour}:00 — ${fmt(cell.value)} requests`}
                                />
                            ))}
                        </div>
                    ))}
                    <div className="flex items-center justify-between mt-3 pt-3 border-t text-xs text-muted">
                        <span>Hour of Day (UTC)</span>
                        <div className="flex items-center gap-2">
                            <div className="flex items-center gap-1"><div className="w-3 h-3 bg-surface-alt rounded-sm border" /> Low</div>
                            <div className="flex items-center gap-1"><div className="w-3 h-3 bg-blue-300 rounded-sm" /> Med</div>
                            <div className="flex items-center gap-1"><div className="w-3 h-3 bg-blue-600 rounded-sm" /> High</div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
};


// ============================================================================
// Tab definitions
// ============================================================================

// There is no Overview or Alerts tab, deliberately: the card
// dashboard IS the overview, and alerts live in its bell drawer. Traffic
// (tab id `usage`) leads — it is the drill-down this view exists for.
const TABS: (TabDef & { id: TabId })[] = [
    { id: 'usage',        label: 'Traffic',     icon: <Activity className="w-4 h-4" /> },
    { id: 'cost',         label: 'Cost',        icon: <DollarSign className="w-4 h-4" /> },
    { id: 'economics',    label: 'LLM Economics', icon: <TrendingUp className="w-4 h-4" /> },
    { id: 'governance',   label: 'Governance',  icon: <Shield className="w-4 h-4" /> },
    { id: 'agents',       label: 'Agents',      icon: <Bot className="w-4 h-4" /> },
    { id: 'reliability',  label: 'Reliability', icon: <Zap className="w-4 h-4" /> },
];


// ============================================================================
// Main Component
// ============================================================================

/** The reads of this page; each fails on its own. */
type AnalyticsPart =
    | 'servers' | 'realtime' | 'performance' | 'dashboard' | 'stats' | 'usageAlerts'
    | 'models' | 'requests' | 'latency' | 'errorRate' | 'heatmap'
    | 'fleet' | 'scope' | 'runStats' | 'scopedResources'
    | 'costBreakdown' | 'costBudgets' | 'costSeries' | 'costMonthly' | 'costSavings'
    | 'posture' | 'compliance' | 'agents';

/** What the page says of a read that failed, before what the other side said. */
const ANALYTICS_PART_FAILED: Record<AnalyticsPart, string> = {
    servers: 'Failed to load the health of the MCP servers',
    realtime: 'Failed to load the live metrics',
    performance: 'Failed to load the performance metrics',
    dashboard: 'Failed to load the KPI figures',
    stats: 'Failed to load the statistics of the gateway',
    usageAlerts: 'Failed to load the quota breaches',
    models: 'Failed to load the health of the AI models',
    requests: 'Failed to load the request trend',
    latency: 'Failed to load the latency trend',
    errorRate: 'Failed to load the error-rate trend',
    heatmap: 'Failed to load the hourly heatmap',
    fleet: 'Failed to load the AI Systems; the scope filter may be incomplete',
    scope: 'Failed to resolve the group this page was opened for; the page is not scoped to it',
    runStats: 'Failed to load the run statistics of the selected system',
    scopedResources: 'Failed to load the resources of the selected systems; the health of every resource of the tenant is shown',
    costBreakdown: 'Failed to load the cost breakdown',
    costBudgets: 'Failed to load the budgets',
    costSeries: 'Failed to load the cost trend',
    costMonthly: 'Failed to load the monthly cost',
    costSavings: 'Failed to load the cost savings',
    posture: 'Failed to load the governance posture',
    compliance: 'Failed to load the compliance coverage',
    agents: 'Failed to load the agent activity',
};

/** The reads whose cards each tab holds: the tab shows the failures of these. */
const ANALYTICS_TAB_PARTS: Record<string, AnalyticsPart[]> = {
    usage: ['performance', 'realtime', 'requests', 'errorRate', 'heatmap'],
    reliability: ['performance', 'latency', 'dashboard', 'stats', 'servers', 'models', 'scopedResources'],
    cost: ['costBreakdown', 'costBudgets', 'costSeries', 'runStats'],
    economics: ['performance', 'costMonthly', 'costSavings'],
    governance: ['posture', 'compliance', 'usageAlerts'],
    agents: ['agents'],
};

const MissionControl: React.FC<MissionControlProps> = ({ className = '', setActiveTab: setAppTab, initialGroupId }) => {
    // ── Tab state ──
    // A tab is one of this page's own or one contributed to the
    // `mission-control.tabs` slot (Caching is contributed).
    const slotTabs = useSlot('mission-control.tabs');
    const performanceTiles = useSlot('mission-control.performance-tiles');
    const governanceSections = useSlot('mission-control.governance-sections');
    const { tabs, contributed } = useMemo(() => {
        const merged = mergeTabs(TABS, slotTabs);
        return {
            tabs: merged.tabs.map((t): TabDef =>
                'render' in t ? { id: t.id, label: t.label, icon: <t.icon className="w-4 h-4" /> } : t,
            ),
            contributed: merged.contributed,
        };
    }, [slotTabs]);
    const [activeTab, setActiveTab] = useState<string>('usage');

    // ── Global controls ──
    // Persisted, and SHARED with Analytics / AI Systems / Resource Groups —
    // an operator chasing a 90-day cost spike shouldn't have to re-pick the
    // window on every surface. Mission Control offers two options nobody
    // else does (MTD, last month); those stay selected here even after a
    // detour through a surface that can only do rolling windows.
    const [selectedPeriod, setSelectedPeriod] = useTimespanPreference(
        MISSION_CONTROL_TIMESPANS,
        '24h',
        isCustomRange,
    );
    // Custom calendar-range popover state (dates are YYYY-MM-DD, end inclusive).
    const [customOpen, setCustomOpen] = useState(false);
    const [customStart, setCustomStart] = useState('');
    const [customEnd, setCustomEnd] = useState('');
    // Provenance plane: governed inline traffic vs observed (imported) telemetry
    // vs all. Drives the proxy_metrics.source / usage_logs.source filter.
    const [plane, setPlane] = useLocalStorageString('mission-control-plane', 'all');
    // Optional scope to a single Resource Group (an AI System is an
    // RG with group_type='ai_system'). 'all' = no filter. Only the usage_logs /
    // proxy_logs-backed tabs (Cost, Governance, Agents) can honour this —
    // proxy_metrics carries no group id, so the infra tabs (Overview / Usage /
    // Reliability) leave the control disabled.
    const [groupFilterId, setGroupFilterId] = useLocalStorageString('mission-control-group-filter', 'all');
    // A dashboard deep-link ("Metrics" on a card) overrides whatever scope
    // was persisted from the operator's last visit. Applied once per
    // deep-link (see the fleet-aware effect below) so it doesn't fight
    // manual scope changes afterwards.
    const appliedDeepLinkRef = useRef<string | null>(null);
    // The selection may be one id, several (comma-joined), or 'all'.
    const selectedSystemIds = React.useMemo(
        () => (groupFilterId === 'all' ? [] : groupFilterId.split(',').filter(Boolean)),
        [groupFilterId],
    );
    const singleSystemId = selectedSystemIds.length === 1 ? selectedSystemIds[0] : null;
    const [systemPickerOpen, setSystemPickerOpen] = useState(false);
    const [exportMenuOpen, setExportMenuOpen] = useState(false);
    const [groupFilterOptions, setGroupFilterOptions] = useState<{ id: string; name: string }[]>([]);
    // The AI System fleet — one fetch serves the
    // scope selector, the Overview fleet board and the assurance band's
    // identity chips. Health comes from the materialised rollup rows.
    const [fleet, setFleet] = useState<AISystemFleetResponse | null>(null);
    // Run-ledger signals for the selected system + period (cost per
    // completed run, outcome mix). Shared with the StatusStrip so its
    // coverage chip describes the same window as the cards.
    const [runStats, setRunStats] = useState<RunStatsResponse | null>(null);
    const [runStatsLoading, setRunStatsLoading] = useState(false);
    // Auto-refresh is opt-in. Defaulting it on meant every idle Mission
    // Control tab re-ran the full metrics fan-out twice a minute — and at
    // long timespans the operator watched the dashboard redraw under
    // them. The choice persists per browser.
    const [autoRefreshRaw, setAutoRefreshRaw] = useLocalStorageString('mission-control-auto-refresh', '0');
    const autoRefresh = autoRefreshRaw === '1';
    const setAutoRefresh = (v: boolean) => setAutoRefreshRaw(v ? '1' : '0');
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [lastUpdate, setLastUpdate] = useState<Date | null>(null);
    // Cost (FinOps) + Governance tab data — loaded lazily when their tab is
    // active (and on period/plane change). Separate from the proxy_metrics
    // state because they read usage_logs / proxy_logs aggregations.
    const [costData, setCostData] = useState<any>(null);
    // Cost composition over time (daily; rolled up client-side) + the
    // Economics tab's "cost avoided" panel.
    const [costSeries, setCostSeries] = useState<any>(null);
    const [costSavings, setCostSavings] = useState<any>(null);
    const [costBudgets, setCostBudgets] = useState<any>(null);
    const [costMonthly, setCostMonthly] = useState<{ data: MonthlyCostPoint[] } | null>(null);
    const [costUserFilter, setCostUserFilter] = useState('');
    const [econLoading, setEconLoading] = useState(false);
    const [costLoading, setCostLoading] = useState(false);
    const [governanceData, setGovernanceData] = useState<any>(null);
    const [complianceCards, setComplianceCards] = useState<any[]>([]);
    const [govLoading, setGovLoading] = useState(false);
    const [agentActivity, setAgentActivity] = useState<any>(null);
    const [agentLoading, setAgentLoading] = useState(false);
    // Ticks on every auto-refresh (and manual Refresh). The per-tab data loaders
    // (Cost / Governance / Agents) below depend on it so they re-fetch on the
    // 30s cycle — otherwise a tab loads exactly ONCE on entry and a transient
    // first-load failure leaves it blank ($0.00 / 0) forever (keep-last-good
    // can't help when there was never a good first load). This makes those
    // panels self-heal like the main dashboard.
    const [refreshTick, setRefreshTick] = useState(0);
    const refreshTimer = useRef<ReturnType<typeof setInterval> | null>(null);

    // ── State ──
    const [state, setState] = useState<MissionControlState>({
        usageAlerts: [],
        realTime: null,
        recentActivity: null,
        performance: null,
        errors: null,
        mcpMethods: null,
        mcpToolCalls: null,
        topMcpServers: [],
        topLlmModels: [],
        a2aCapabilities: null,
        topA2aAgents: [],
        a2aOverview: null,
        usagePatterns: null,
        kpis: {
            requestsLastHour: 0, requestsLastDay: 0, activeConnections: 0,
            errorRate: 0, avgResponseTime: 0, uptime: 0,
        },
        serverHealth: [],
        aiModelHealth: [],
        requestsTimeSeries: [],
        latencyTimeSeries: [],
        errorsTimeSeries: [],
        heatmapData: null,
        llmEconomics: null,
        mediaUsage: null,
    });

    const {
        fetchServers,
        gatewayClient,
        isAuthenticated,
        fetchAllUsageAlerts,
        fetchPerformanceMetrics,
        fetchRealTimeMetrics,
        fetchDashboardMetrics,
        exportMetricsData,
    } = useGateway();


    // Every read of this page fails on its own: the cards it feeds keep what
    // they showed, and the tab that holds them says which read failed.
    const [readFailure, reads] = useReadFailures<AnalyticsPart>();

    // ── Load all data ──
    const hasLoadedOnceRef = useRef(false);
    // Monotonic sequence for the main fan-out. The per-tab sub-effects below
    // guard with a `cancelled` flag; this one — the fan-out on the 30s
    // timer, and the one whose Promise.allSettled can outlast that interval at
    // 90 days / All Time — guards with a sequence. Without it a slow load for
    // the old period would repaint the KPIs after the new one has landed, and a
    // load in flight at unmount would write state. The newest sequence alone
    // may write.
    const loadSeqRef = useRef(0);
    const loadAllData = useCallback(async (isManual = false) => {
        if (!isAuthenticated || !gatewayClient) {
            setLoading(false);
            return;
        }

        const seq = ++loadSeqRef.current;

        if (isManual) setRefreshing(true);
        // First-ever load only. Testing `lastUpdate` here would not do: that
        // state isn't in this callback's dependency list, so the closure
        // would capture the initial `null` forever — and every 30s
        // auto-refresh would re-enter the full-screen "Loading Mission
        // Control…" state for the whole fan-out. At 24h that is a flash; at
        // 90 days / All Time the fan-out outlasts the refresh interval and
        // the dashboard looks permanently stuck loading. A ref reads
        // the CURRENT value without dep churn (adding `lastUpdate` to the
        // deps would re-arm the refresh timer on every tick instead).
        else if (!hasLoadedOnceRef.current) setLoading(true);

        try {
            const periodHours = periodToHours(selectedPeriod);
            const intervalMin = intervalForPeriod(selectedPeriod);
            // Calendar ranges (MTD / last month / custom) travel as explicit
            // start/end and override period_hours server-side.
            const range = periodToRange(selectedPeriod);
            // 'all' plane => omit the param (no source filter server-side).
            const planeParam = plane === 'all' ? undefined : plane;

            // ── Essential data (parallel) ──
            // `systemAlertsResult` is the server-fed system_alerts feed
            // (downstream failures, health transitions, infra thresholds,
            // internal errors). "MCP Server X unhealthy" and the like
            // arrive in it as `resource_health` alerts raised by the Rust
            // proxy, deduped + persisted across refresh; nothing is
            // derived client-side from the health payload.
            const [
                serversResult, realTimeResult, performanceResult,
                dashboardResult, statsResult, usageAlertsResult,
            ] = await Promise.allSettled([
                fetchServers(),
                fetchRealTimeMetrics({ last_minutes: 5 }),
                fetchPerformanceMetrics({ period_hours: range.period_hours, start: range.start, end: range.end, plane: planeParam }),
                fetchDashboardMetrics(),
                gatewayClient.fetchStats(),
                // Quota breaches — the Governance tab's breach panel reads
                // these; acknowledge lives in the dashboard's alert drawer.
                fetchAllUsageAlerts({ status: 'active' }),
            ]);

            // ── Charts & enrichment (parallel, non-blocking) ──
            const [
                llmsResult, requestsTsResult, latencyTsResult, errorsTsResult, heatmapResult,
            ] = await Promise.allSettled([
                gatewayClient.listLLMs(),
                gatewayClient.fetchRequestsTimeSeries({
                    period_hours: range.period_hours, start: range.start, end: range.end,
                    interval_minutes: intervalMin, plane: planeParam,
                }),
                gatewayClient.fetchLatencyTimeSeries({
                    period_hours: range.period_hours, start: range.start, end: range.end,
                    interval_minutes: intervalMin, plane: planeParam,
                }),
                gatewayClient.fetchErrorRateTimeSeries({
                    period_hours: range.period_hours, start: range.start, end: range.end,
                    interval_minutes: intervalMin, plane: planeParam,
                }),
                gatewayClient.fetchHourlyHeatmap({ days: (periodHours !== 0 && periodHours <= 24) ? 7 : 30, plane: planeParam }),
            ]);

            // ── Process results ──
            // No alert is synthesised here when a server or model shows up
            // as unhealthy: a local list would be ephemeral (refresh = gone)
            // and leave no audit trail. Those events are `resource_health`
            // rows in `system_alerts`, raised by the Rust proxy, with
            // occurrence_count, ack/resolve, and auto-resolution.
            // The UI simply renders whatever the server returns.
            const update: Partial<MissionControlState> = {};

            // Servers — map health into the UI shape. No alert synthesis.
            if (serversResult.status === 'fulfilled' && serversResult.value) {
                const srvList = serversResult.value;
                update.serverHealth = srvList.map(s => ({ id: s.id, name: s.name, health: s.health_status }));
            }

            // AI Models — same: shape for the UI, no synthesis.
            if (llmsResult.status === 'fulfilled' && llmsResult.value) {
                const modelsObj = llmsResult.value.models || {};
                const enabled = Object.values(modelsObj).filter((m: any) => m.enabled);
                update.aiModelHealth = enabled.map((m: any) => {
                    const st = m.health_status?.status || 'unknown';
                    return {
                        id: m.id, name: m.name, provider: m.provider,
                        status: st as AIModelHealth['status'],
                        responseTime: m.health_status?.avg_latency_ms,
                        lastCheck: m.health_status?.last_check, error: m.health_status?.error_message,
                    };
                });
            }

            // Real-time
            if (realTimeResult.status === 'fulfilled') {
                const rt = realTimeResult.value;
                update.realTime = rt.real_time_metrics?.real_time || null;
                update.recentActivity = rt.real_time_metrics?.recent_activity || null;
            }

            // Performance
            if (performanceResult.status === 'fulfilled') {
                const pm = performanceResult.value.performance_metrics;
                update.performance = pm?.overview || pm?.performance || null;
                if (pm?.performance && update.performance) {
                    update.performance = { ...update.performance, ...pm.performance };
                }
                update.errors = pm?.errors || null;
                update.mcpMethods = pm?.mcp_methods || null;
                update.mcpToolCalls = pm?.mcp_tool_calls || null;
                update.topMcpServers = pm?.top_mcp_servers || [];
                update.topLlmModels = pm?.top_llm_models || [];
                update.a2aCapabilities = pm?.a2a_capabilities || null;
                update.topA2aAgents = pm?.top_a2a_agents || [];
                update.a2aOverview = pm?.a2a_overview || null;
                update.usagePatterns = pm?.usage_patterns || null;
                update.llmEconomics = pm?.llm_economics || null;
                update.mediaUsage = pm?.media_usage || null;
            }

            // Dashboard KPIs
            const kpis: DashboardKPIs = {
                requestsLastHour: 0, requestsLastDay: 0, activeConnections: 0,
                errorRate: 0, avgResponseTime: 0, uptime: 0,
            };
            if (dashboardResult.status === 'fulfilled' && dashboardResult.value) {
                const d = dashboardResult.value.dashboard;
                kpis.requestsLastHour = d.requests_last_hour || 0;
                kpis.requestsLastDay = d.requests_last_day || 0;
                kpis.activeConnections = d.active_connections || 0;
                kpis.errorRate = d.error_rate || 0;
                kpis.avgResponseTime = d.avg_response_time_ms || 0;
                kpis.cpuUsage = d.cpu_usage_percent;
                kpis.memoryUsage = d.memory_usage_percent;
            }
            if (statsResult.status === 'fulfilled' && statsResult.value) {
                kpis.uptime = statsResult.value.uptime || 0;
                kpis.activeConnections = kpis.activeConnections || statsResult.value.active_connections || 0;
                kpis.errorRate = kpis.errorRate || statsResult.value.error_rate || 0;
            }
            // Dashboard-derived alerts — ephemeral, current-refresh only.
            // These are *hints* for the live dashboard (error rate, CPU),
            // not persisted alerts. A proper monitor watcher would raise
            // them through the Rust `services::system_alerts` helper so
            // they'd show up in the system_alerts feed below instead.
            // The figures of a read that failed stay as they were: a KPI row
            // of zeros, or an emptied list of breaches, would be a statement.
            if (dashboardResult.status === 'fulfilled' || !hasLoadedOnceRef.current) update.kpis = kpis;
            if (usageAlertsResult.status === 'fulfilled') update.usageAlerts = usageAlertsResult.value?.alerts || [];

            // Time series
            if (requestsTsResult.status === 'fulfilled' && requestsTsResult.value) { update.requestsTimeSeries = requestsTsResult.value.data || []; }
            if (latencyTsResult.status === 'fulfilled' && latencyTsResult.value) { update.latencyTimeSeries = latencyTsResult.value.data || []; }
            if (errorsTsResult.status === 'fulfilled' && errorsTsResult.value) { update.errorsTimeSeries = errorsTsResult.value.data || []; }
            if (heatmapResult.status === 'fulfilled' && heatmapResult.value) { update.heatmapData = heatmapResult.value; }

            if (seq !== loadSeqRef.current) return;

            const settled: [AnalyticsPart, PromiseSettledResult<unknown>][] = [
                ['servers', serversResult], ['realtime', realTimeResult], ['performance', performanceResult],
                ['dashboard', dashboardResult], ['stats', statsResult], ['usageAlerts', usageAlertsResult],
                ['models', llmsResult], ['requests', requestsTsResult], ['latency', latencyTsResult],
                ['errorRate', errorsTsResult], ['heatmap', heatmapResult],
            ];
            for (const [part, result] of settled) {
                if (result.status === 'fulfilled') reads.loaded(part);
                else reads.failed(part, ANALYTICS_PART_FAILED[part], result.reason);
            }

            setState(prev => ({ ...prev, ...update }));
            setLastUpdate(new Date());
            hasLoadedOnceRef.current = true;
            setError(null);
        } catch (err) {
            if (seq !== loadSeqRef.current) return;
            console.error('Mission Control load error:', errorKind(err));
            setError(err instanceof Error ? err.message : 'Failed to load dashboard data');
        } finally {
            // A superseded load must not clear the spinner the newer one set.
            if (seq === loadSeqRef.current) {
                setLoading(false);
                setRefreshing(false);
            }
        }
    }, [
        isAuthenticated, gatewayClient, selectedPeriod, plane,
        fetchServers, fetchRealTimeMetrics, fetchPerformanceMetrics,
        fetchDashboardMetrics, fetchAllUsageAlerts, reads,
    ]);

    // Bumping the sequence on cleanup retires whatever is still in flight, so
    // an unmount (or a period change) can't be followed by a late setState.
    useEffect(() => {
        runBackground(loadAllData(), 'all data');
        return () => { loadSeqRef.current += 1; };
    }, [loadAllData]);

    useEffect(() => {
        if (refreshTimer.current) clearInterval(refreshTimer.current);
        if (autoRefresh) { refreshTimer.current = setInterval(() => { runBackground(loadAllData(), 'all data'); setRefreshTick(t => t + 1); }, 30000); }
        return () => { if (refreshTimer.current) clearInterval(refreshTimer.current); };
    }, [autoRefresh, loadAllData]);


    // The AI System fleet — feeds the global scope dropdown, the Overview
    // fleet board AND the assurance band. One request with the materialised
    // health rollup attached (include_health reads ai_system_health in the
    // same query — no per-system fan-out). Best-effort: an empty fleet just
    // disables the filter and renders the "no systems yet" lead.
    useEffect(() => {
        if (!isAuthenticated || !gatewayClient) return;
        let cancelled = false;
        gatewayClient.fetchAISystems({ include_health: true, limit: 200 })
            .then((f) => {
                if (cancelled) return;
                setFleet(f);
                setGroupFilterOptions(
                    (f?.systems || []).map((s) => ({ id: s.id, name: s.display_name || s.name }))
                );
                reads.loaded('fleet');
            })
            .catch((err: unknown) => {
                // The fleet and the scope filter keep what they held.
                if (!cancelled) reads.failed('fleet', ANALYTICS_PART_FAILED.fleet, err);
            });
        return () => { cancelled = true; };
    }, [isAuthenticated, gatewayClient, selectedPeriod, refreshTick, reads]);

    // Self-heal a stale persisted scope. The selector id lives in
    // localStorage and survives DB resets — after one, it names a system
    // that no longer exists and every scoped panel would quietly render
    // empty. Once the fleet is known, an unknown id falls back to "all".
    useEffect(() => {
        if (!fleet || groupFilterId === 'all') return;
        const known = new Set(fleet.systems.map((s) => s.id));
        const kept = groupFilterId.split(',').filter((id) => known.has(id));
        if (kept.length !== groupFilterId.split(',').filter(Boolean).length) {
            setGroupFilterId(kept.length ? kept.join(',') : 'all');
        }
    }, [fleet, groupFilterId, setGroupFilterId]);

    // Apply a dashboard deep-link ("Metrics" on a card). A system card
    // passes its own id, which the selector holds directly. An org card
    // passes a plain resource-group id the selector cannot hold — resolve
    // it through the hierarchy tree to the AI Systems in that subtree, so
    // the operator lands scoped to exactly the systems the card was
    // rolling up. Waits for the fleet: setting an unknown id earlier
    // would just be stripped by the stale-scope self-heal above.
    useEffect(() => {
        if (!initialGroupId || !fleet || !gatewayClient) return;
        if (appliedDeepLinkRef.current === initialGroupId) return;
        appliedDeepLinkRef.current = initialGroupId;
        const systemIds = new Set(fleet.systems.map((s) => s.id));
        if (systemIds.has(initialGroupId)) {
            setGroupFilterId(initialGroupId);
            return;
        }
        let cancelled = false;
        gatewayClient.fetchGroupHierarchyTree()
            .then((resp) => {
                if (cancelled) return;
                const findNode = (nodes: GroupHierarchyTree[]): GroupHierarchyTree | null => {
                    for (const n of nodes) {
                        if (n.id === initialGroupId) return n;
                        const hit = findNode(n.children || []);
                        if (hit) return hit;
                    }
                    return null;
                };
                const collect = (n: GroupHierarchyTree, acc: string[]) => {
                    if (systemIds.has(n.id)) acc.push(n.id);
                    (n.children || []).forEach((c) => collect(c, acc));
                };
                const node = findNode(resp.tree || []);
                if (!node) return; // unknown group — leave the persisted scope alone
                const ids: string[] = [];
                collect(node, ids);
                if (ids.length) setGroupFilterId(ids.join(','));
                reads.loaded('scope');
            })
            .catch((err: unknown) => {
                // The page renders unscoped, and says it was asked for a group.
                if (!cancelled) reads.lost('scope', ANALYTICS_PART_FAILED.scope, err);
            });
        return () => { cancelled = true; };
    }, [initialGroupId, fleet, gatewayClient, setGroupFilterId, reads]);

    // Run-ledger signals for the selected AI System — the Cost tab's
    // per-completed-run section is the only consumer.
    useEffect(() => {
        if (!isAuthenticated || !gatewayClient) return;
        if (!singleSystemId || activeTab !== 'cost') return;
        let cancelled = false;
        setRunStatsLoading(true);
        gatewayClient.fetchAISystemRunStats(
            singleSystemId,
            periodToRange(selectedPeriod).period_hours,
            { start: periodToRange(selectedPeriod).start, end: periodToRange(selectedPeriod).end },
        )
            .then((s: RunStatsResponse) => {
                if (cancelled) return;
                setRunStats(s);
                reads.loaded('runStats');
            })
            .catch((err: unknown) => {
                if (cancelled) return;
                setRunStats(null);
                reads.lost('runStats', ANALYTICS_PART_FAILED.runStats, err);
            })
            .finally(() => { if (!cancelled) setRunStatsLoading(false); });
        return () => { cancelled = true; };
    }, [isAuthenticated, gatewayClient, singleSystemId, activeTab, selectedPeriod, refreshTick, reads]);

    // The selection's composition — which models and MCP servers the
    // selected AI Systems actually depend on. proxy_metrics carries no
    // group id, so request-rate KPIs can never be scoped; RESOURCE health
    // can, because bindings are exactly what an AI System is made of.
    const [scopedResources, setScopedResources] = useState<
        { modelIds: Set<string>; serverIds: Set<string> } | null
    >(null);
    useEffect(() => {
        if (!isAuthenticated || !gatewayClient) return;
        if (selectedSystemIds.length === 0) { setScopedResources(null); return; }
        let cancelled = false;
        Promise.all(selectedSystemIds.map((id) => Promise.all([
            gatewayClient.fetchEffectiveLLMModels(id),
            gatewayClient.fetchEffectiveServerConfigs(id),
        ])))
            .then((per) => {
                if (cancelled) return;
                const modelIds = new Set<string>();
                const serverIds = new Set<string>();
                for (const [models, configs] of per) {
                    for (const m of [...((models as any)?.direct_models ?? []),
                                     ...((models as any)?.inherited_models ?? [])]) {
                        if (m?.id) modelIds.add(m.id);
                    }
                    for (const c of [...((configs as any)?.direct_configs ?? []),
                                     ...((configs as any)?.inherited_configs ?? [])]) {
                        // The health feed is keyed by MCP server, the binding by
                        // server *config* — the nested server carries the join.
                        if (c?.mcp_server?.id) serverIds.add(c.mcp_server.id);
                    }
                }
                setScopedResources({ modelIds, serverIds });
                reads.loaded('scopedResources');
            })
            .catch((err: unknown) => {
                // A set with a system's resources missing would hide them
                // from the health lists: unscoped, and said.
                if (cancelled) return;
                setScopedResources(null);
                reads.lost('scopedResources', ANALYTICS_PART_FAILED.scopedResources, err);
            });
        return () => { cancelled = true; };
    }, [isAuthenticated, gatewayClient, groupFilterId, selectedSystemIds, refreshTick, reads]);

    // Cost (FinOps) tab data — usage_logs aggregations. Reloads on plane/period/group.
    useEffect(() => {
        if (activeTab !== 'cost' || !isAuthenticated || !gatewayClient) return;
        const range = periodToRange(selectedPeriod);
        const planeParam = plane === 'all' ? undefined : plane;
        const groupParam = groupFilterId === 'all' ? undefined : groupFilterId;
        setCostLoading(true);
        runBackground(Promise.allSettled([
            gatewayClient.fetchCostBreakdown({
                period_hours: range.period_hours, start: range.start, end: range.end,
                plane: planeParam, group_id: groupParam, top_n: 50,
                // The preceding equal window powers the delta chips —
                // "what changed" is the first FinOps question.
                compare: true,
            }),
            gatewayClient.fetchCostBudgets(),
            gatewayClient.fetchCostSeries({
                dimension: 'provider',
                period_hours: range.period_hours, start: range.start, end: range.end,
                plane: planeParam, group_id: groupParam,
            }),
        ]).then(([bd, bg, cs]) => {
            // The last data stays on a failed read instead of blanking (a
            // failed 30s auto-refresh would otherwise read as "Total Spend
            // $0.00"), and the tab marks it as stale.
            if (cs.status === 'fulfilled') { setCostSeries(cs.value); reads.loaded('costSeries'); }
            else reads.failed('costSeries', ANALYTICS_PART_FAILED.costSeries, cs.reason);
            if (bd.status === 'fulfilled') { setCostData(bd.value); reads.loaded('costBreakdown'); }
            else reads.failed('costBreakdown', ANALYTICS_PART_FAILED.costBreakdown, bd.reason);
            if (bg.status === 'fulfilled') { setCostBudgets(bg.value); reads.loaded('costBudgets'); }
            else reads.failed('costBudgets', ANALYTICS_PART_FAILED.costBudgets, bg.reason);
        }).finally(() => setCostLoading(false)), 'all settled');
    }, [activeTab, selectedPeriod, plane, groupFilterId, isAuthenticated, gatewayClient, refreshTick, reads]);

    // LLM Economics tab — monthly series + cost savings. The monthly series is
    // always the last 12 calendar months: the unit-cost trend is a story told
    // across months, so it deliberately ignores the header's period selector.
    // The savings fetch does NOT get that exemption — it consumes the range, so
    // `selectedPeriod` must stay in the deps below. Without it the Cost
    // Savings panel reports whichever period was live when the tab first
    // mounted while every neighbouring tile moves with the selector.
    useEffect(() => {
        if (activeTab !== 'economics' || !isAuthenticated || !gatewayClient) return;
        const planeParam = plane === 'all' ? undefined : plane;
        const groupParam = groupFilterId === 'all' ? undefined : groupFilterId;
        const range = periodToRange(selectedPeriod);
        setEconLoading(true);
        runBackground(Promise.allSettled([
            gatewayClient.fetchCostMonthly({ months: 12, plane: planeParam, group_id: groupParam }),
            gatewayClient.fetchCostSavings({
                period_hours: range.period_hours, start: range.start, end: range.end,
                group_id: groupParam,
            }),
        ]).then(([m, sv]) => {
            if (m.status === 'fulfilled') { setCostMonthly(m.value); reads.loaded('costMonthly'); }
            else reads.failed('costMonthly', ANALYTICS_PART_FAILED.costMonthly, m.reason);
            if (sv.status === 'fulfilled') { setCostSavings(sv.value); reads.loaded('costSavings'); }
            else reads.failed('costSavings', ANALYTICS_PART_FAILED.costSavings, sv.reason);
        }).finally(() => setEconLoading(false)), 'all settled');
    }, [activeTab, selectedPeriod, plane, groupFilterId, isAuthenticated, gatewayClient, refreshTick, reads]);

    // Governance tab data — proxy_logs posture + shadow-AI, plus reused
    // compliance-coverage and budget/quota-breach feeds.
    useEffect(() => {
        if (activeTab !== 'governance' || !isAuthenticated || !gatewayClient) return;
        const range = periodToRange(selectedPeriod);
        const groupParam = groupFilterId === 'all' ? undefined : groupFilterId;
        setGovLoading(true);
        runBackground(Promise.allSettled([
            gatewayClient.fetchGovernancePosture({
                period_hours: range.period_hours, start: range.start, end: range.end,
                group_id: groupParam,
            }),
            gatewayClient.getComplianceDashboard(),
        ]).then(([gp, cd]) => {
            // Keep last-good data on a transient failure (same rationale as the
            // cost tab) so a single failed poll doesn't blank Enforcement
            // Outcomes / Compliance Coverage.
            if (gp.status === 'fulfilled') { setGovernanceData(gp.value); reads.loaded('posture'); }
            else reads.failed('posture', ANALYTICS_PART_FAILED.posture, gp.reason);
            if (cd.status === 'fulfilled') { setComplianceCards(cd.value?.cards || []); reads.loaded('compliance'); }
            else reads.failed('compliance', ANALYTICS_PART_FAILED.compliance, cd.reason);
        }).finally(() => setGovLoading(false)), 'all settled');
    }, [activeTab, selectedPeriod, groupFilterId, isAuthenticated, gatewayClient, refreshTick, reads]);

    // Agents tab — per-agent runtime-authz activity from proxy_logs.
    useEffect(() => {
        if (activeTab !== 'agents' || !isAuthenticated || !gatewayClient) return;
        const periodHours = periodToHours(selectedPeriod);
        const groupParam = groupFilterId === 'all' ? undefined : groupFilterId;
        setAgentLoading(true);
        gatewayClient.fetchAgentActivity({ period_hours: periodHours, group_id: groupParam })
            .then((d: any) => {
                setAgentActivity(d);
                reads.loaded('agents');
            })
            .catch((err: unknown) => reads.failed('agents', ANALYTICS_PART_FAILED.agents, err))
            .finally(() => setAgentLoading(false));
    }, [activeTab, selectedPeriod, groupFilterId, isAuthenticated, gatewayClient, refreshTick, reads]);

    const refreshGovernance = async () => {
        if (!gatewayClient) return;
        try {
            const gp = await gatewayClient.fetchGovernancePosture({
                period_hours: periodToHours(selectedPeriod),
                group_id: groupFilterId === 'all' ? undefined : groupFilterId,
            });
            setGovernanceData(gp);
            reads.loaded('posture');
        } catch (err) {
            reads.failed('posture', ANALYTICS_PART_FAILED.posture, err);
        }
    };

    const handleExportFocus = async () => {
        try {
            const range = periodToRange(selectedPeriod);
            const csv = await gatewayClient.fetchCostFocusCsv({
                period_hours: range.period_hours, start: range.start, end: range.end,
                plane: plane === 'all' ? undefined : plane,
                group_id: groupFilterId === 'all' ? undefined : groupFilterId,
            });
            downloadBlob(
                `brutor-focus-${new Date().toISOString().split('T')[0]}.csv`,
                new Blob([csv], { type: 'text/csv;charset=utf-8' }),
            );
        } catch (err) {
            console.error('FOCUS export failed:', errorKind(err));
            setError(failureSentence('Failed to export FOCUS CSV', err));
        }
    };

    const handleExportStatement = async () => {
        if (!singleSystemId) return;
        try {
            // The statement is monthly: use the selected calendar month when
            // the period is one (MTD / Last Mo / a custom range inside one
            // month); otherwise default to the previous full month.
            const range = periodToRange(selectedPeriod);
            let month: string;
            if (range.start && (selectedPeriod === 'mtd' || selectedPeriod === 'lastm'
                || (range.end && range.start.slice(0, 7) === new Date(new Date(range.end).getTime() - 1).toISOString().slice(0, 7)))) {
                month = range.start.slice(0, 7);
            } else {
                const now = new Date();
                month = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, 1))
                    .toISOString().slice(0, 7);
            }
            const st = await gatewayClient.fetchAISystemStatement(singleSystemId, month);
            const name = (st.system?.name || singleSystemId).replace(/[^a-z0-9-]+/gi, '-');
            downloadBlob(
                `${name}-statement-${month}.html`,
                new Blob([statementHtml(st)], { type: 'text/html;charset=utf-8' }),
            );
        } catch (err) {
            console.error('Statement export failed:', errorKind(err));
            setError(failureSentence('Failed to generate the monthly statement', err));
        }
    };

    const handleExport = async () => {
        try {
            const data = await exportMetricsData({
                format: 'json', period_hours: periodToHours(selectedPeriod),
                plane: plane === 'all' ? undefined : plane,
            });
            const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a'); a.href = url;
            a.download = `metrics-export-${new Date().toISOString().split('T')[0]}.json`;
            document.body.appendChild(a); a.click(); document.body.removeChild(a);
            URL.revokeObjectURL(url);
        } catch (err) { console.error('Export failed:', errorKind(err)); setError(failureSentence('Failed to export metrics data', err)); }
    };

    // ── Derived values ──
    const { kpis } = state;
    // Resource health follows the AI-System selection: an operator scoped to
    // one system wants ITS models and tools, not the tenant's whole estate.
    // (Falls back to everything while the composition is still loading, and
    // whenever the scope is "All AI Systems".)
    const serverHealth = scopedResources
        ? state.serverHealth.filter(s => scopedResources.serverIds.has(s.id))
        : state.serverHealth;
    const aiModelHealth = scopedResources
        ? state.aiModelHealth.filter(m => scopedResources.modelIds.has(m.id))
        : state.aiModelHealth;
    const healthyServers = serverHealth.filter(s => serverHealthReading(s.health).state === 'healthy').length;
    const healthyModels = aiModelHealth.filter(m => m.status === 'healthy').length;
    const totalResources = serverHealth.length + aiModelHealth.length;
    const healthyResources = healthyServers + healthyModels;
    const systemHealthPct = totalResources > 0 ? (healthyResources / totalResources) * 100 : 0;
    const systemStatus = systemHealthPct === 100 ? 'healthy' : systemHealthPct >= 50 ? 'degraded' : 'critical';

    // ============================================================================
    // Loading state
    // ============================================================================

    if (loading) {
        return (
            <div className="h-full flex items-center justify-center">
                <div className="text-center">
                    <RefreshCw className="w-8 h-8 animate-spin text-primary mx-auto mb-4" />
                    <p className="text-secondary">Loading Mission Control...</p>
                </div>
            </div>
        );
    }


    // ============================================================================
    // Tab panel renderers
    // ============================================================================

    const renderTraffic = () => (
        <>
            {state.requestsTimeSeries.length > 0 ? (
                <RequestVolumeChart data={state.requestsTimeSeries} />
            ) : (
                <div className="bg-surface rounded-lg border p-6">
                    <h4 className="font-medium text-default mb-4">Request Volume Over Time</h4>
                    <div className="h-[250px] flex items-center justify-center">
                        <p className="text-sm text-muted">No time-series data for this period</p>
                    </div>
                </div>
            )}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-4">
                {state.performance && (
                    <ProxyTypeBreakdownChart mcpMethods={state.mcpMethods}
                        a2aOverview={state.a2aOverview}
                        performance={state.performance} recentActivity={state.recentActivity} />
                )}
                {state.errorsTimeSeries.length > 0 ? (
                    <ErrorRateChart data={state.errorsTimeSeries} />
                ) : (
                    <SuccessRateGauge rate={state.performance?.success_rate || 0} />
                )}
            </div>
            {state.recentActivity?.error_spike && (
                <div className="mt-4 bg-red-50 border border-red-200 rounded-lg p-3 flex items-center gap-2">
                    <AlertTriangle className="w-4 h-4 text-red-500 flex-shrink-0" />
                    <div>
                        <h4 className="text-sm font-medium text-red-800">Error Spike Detected</h4>
                        <p className="text-xs text-red-600">Unusual increase in error rate. Check Performance tab for details.</p>
                    </div>
                </div>
            )}
        </>
    );

    const renderPerformance = () => (
        <>
            {state.latencyTimeSeries.length > 0 ? (
                <LatencyPercentilesChart data={state.latencyTimeSeries} />
            ) : (
                <div className="bg-surface rounded-lg border p-6">
                    <h4 className="font-medium text-default mb-4">Response Time Percentiles</h4>
                    <div className="h-[250px] flex items-center justify-center">
                        <p className="text-sm text-muted">No latency data for this period</p>
                    </div>
                </div>
            )}
            {state.performance?.response_time_percentiles && (
                <div className="grid grid-cols-5 gap-3 mt-4">
                    {Object.entries(state.performance.response_time_percentiles).map(([p, val]) => (
                        <div key={p} className="bg-surface rounded-lg border p-3 text-center">
                            <div className="text-lg font-bold text-default">{fmtDuration(val)}</div>
                            <div className="text-xs text-muted uppercase">{p}</div>
                        </div>
                    ))}
                </div>
            )}
            {state.performance && (
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mt-4">
                    <div className="bg-surface rounded-lg border p-3">
                        <div className="text-xs text-muted mb-1">Data Transferred</div>
                        <div className="text-sm font-bold text-default">{fmtBytes(state.performance.total_data_transferred_mb)}</div>
                        <div className="text-xs text-muted">Avg {fmtBytes(state.performance.avg_response_size_kb ? state.performance.avg_response_size_kb / 1024 : 0)}/resp</div>
                    </div>
                    <div className="bg-surface rounded-lg border p-3">
                        <div className="text-xs text-muted mb-1">Session Reuse</div>
                        <div className="text-sm font-bold text-green-600">{(state.performance.session_reuse_rate || 0).toFixed(1)}%</div>
                        <div className="text-xs text-muted">Connection efficiency</div>
                    </div>
                    {/* Tiles contributed to the `mission-control.performance-tiles`
                        slot (the cache hit rate is). */}
                    {performanceTiles.map((item) => (
                        <React.Fragment key={item.id}>{item.render({ hours: periodToHours(selectedPeriod) })}</React.Fragment>
                    ))}
                    <div className="bg-surface rounded-lg border p-3">
                        <div className="text-xs text-muted mb-1">Min / Max Latency</div>
                        <div className="text-sm font-bold text-default">{fmtDuration(state.performance.min_duration_ms)} / {fmtDuration(state.performance.max_duration_ms)}</div>
                        <div className="text-xs text-muted">Range for period</div>
                    </div>
                </div>
            )}
        </>
    );

    const renderInfrastructure = () => (
        <div className="app-column h-full">
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 flex-1 min-h-0 min-w-0">
                {/* AI Model Health */}
                <div className="bg-surface rounded-lg border flex flex-col min-h-0">
                    <div className="flex-none p-4 border-b flex items-center gap-2">
                        <Brain className="w-4 h-4 text-purple-600" />
                        <h4 className="text-sm font-semibold text-default">AI Models</h4>
                        <span className="ml-auto text-xs text-muted">{healthyModels}/{aiModelHealth.length} healthy</span>
                    </div>
                    <div className="p-3 space-y-2 scroll-region min-h-0">
                        {aiModelHealth.length === 0 ? (
                            <div className="text-center py-8 text-muted">
                                <Brain className="w-8 h-8 mx-auto mb-2 opacity-40" />
                                <p className="text-sm">No enabled AI models</p>
                                {setAppTab && (
                                    <button onClick={() => setAppTab('llm-models')} className="mt-2 text-xs text-primary hover:underline">
                                        Configure models &rarr;
                                    </button>
                                )}
                            </div>
                        ) : aiModelHealth.map(model => (
                            <div key={model.id} className="flex items-center gap-3 p-2.5 rounded-lg border hover:bg-surface-alt transition-colors">
                                <div className={`w-2 h-2 rounded-full flex-shrink-0 ${statusDot(model.status)}`} />
                                {statusIcon(model.status)}
                                <div className="flex-1 min-w-0 min-h-0">
                                    <div className="flex items-center gap-2">
                                        <span className="text-sm font-medium text-default truncate">{model.name}</span>
                                        <span className="text-xs px-1.5 py-0.5 rounded bg-surface-raised text-muted">{model.provider}</span>
                                    </div>
                                    {model.error ? <p className="text-xs text-red-500 truncate">{model.error}</p>
                                        : model.responseTime ? <p className="text-xs text-muted">{model.responseTime.toFixed(0)}ms latency</p>
                                        : <p className="text-xs text-muted">No recent check</p>}
                                </div>
                                <span className="text-xs text-muted flex-shrink-0">
                                    {model.lastCheck ? new Date(model.lastCheck).toLocaleTimeString() : '\u2014'}
                                </span>
                            </div>
                        ))}
                    </div>
                </div>

                {/* MCP Server Health */}
                <div className="bg-surface rounded-lg border flex flex-col min-h-0">
                    <div className="flex-none p-4 border-b flex items-center gap-2">
                        <Server className="w-4 h-4 text-blue-600" />
                        <h4 className="text-sm font-semibold text-default">MCP Servers</h4>
                        <span className="ml-auto text-xs text-muted">{healthyServers}/{serverHealth.length} healthy</span>
                    </div>
                    <div className="p-3 space-y-2 scroll-region min-h-0">
                        {serverHealth.length === 0 ? (
                            <div className="text-center py-8 text-muted">
                                <Server className="w-8 h-8 mx-auto mb-2 opacity-40" />
                                <p className="text-sm">No MCP servers configured</p>
                                {setAppTab && (
                                    <button onClick={() => setAppTab('mcp-config')} className="mt-2 text-xs text-primary hover:underline">
                                        Add a server &rarr;
                                    </button>
                                )}
                            </div>
                        ) : serverHealth.map(server => (
                            <McpServerHealthRow key={server.id} name={server.name} health={server.health} />
                        ))}
                    </div>
                </div>
            </div>

            {/* Resource utilization */}
            {(kpis.cpuUsage !== undefined || kpis.memoryUsage !== undefined) && (
                <div className="flex-none bg-surface rounded-lg border p-4 mt-4">
                    <h4 className="text-sm font-semibold text-default mb-3 flex items-center gap-2">
                        <HardDrive className="w-4 h-4 text-muted" /> Resource Utilization
                    </h4>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        {kpis.cpuUsage !== undefined && (
                            <UtilizationBar label="CPU" value={kpis.cpuUsage} icon={<Cpu className="w-4 h-4 text-blue-500" />}
                                color={kpis.cpuUsage > 80 ? 'bg-red-500' : kpis.cpuUsage > 60 ? 'bg-yellow-500' : 'bg-blue-500'} />
                        )}
                        {kpis.memoryUsage !== undefined && (
                            <UtilizationBar label="Memory" value={kpis.memoryUsage} icon={<HardDrive className="w-4 h-4 text-purple-500" />}
                                color={kpis.memoryUsage > 80 ? 'bg-red-500' : kpis.memoryUsage > 60 ? 'bg-yellow-500' : 'bg-purple-500'} />
                        )}
                    </div>
                </div>
            )}
        </div>
    );

    const renderLlmEconomics = () => {
        const econ = state.llmEconomics;
        const hasData = econ && (econ.total_tokens > 0 || econ.estimated_cost_usd > 0);
        return (
        <>
            {/* LLM Economics */}
            <div className="mb-6">
                <h3 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3 flex items-center gap-2">
                    <DollarSign className="w-4 h-4" /> LLM Economics
                </h3>
                {hasData ? (
                    <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
                        <div className="bg-surface rounded-lg border p-4">
                            <div className="text-xs text-muted mb-1">Token Usage</div>
                            <div className="text-lg font-bold text-default">{fmtCompact(econ.total_tokens)}</div>
                            <div className="text-xs text-muted mt-1">
                                {fmtCompact(econ.prompt_tokens)} prompt &middot; {fmtCompact(econ.completion_tokens)} completion
                            </div>
                        </div>
                        <div className="bg-surface rounded-lg border p-4">
                            <div className="text-xs text-muted mb-1">Estimated Cost</div>
                            <div className="text-lg font-bold text-green-700">{fmtCost(econ.estimated_cost_usd)}</div>
                            <div className="text-xs text-muted mt-1">
                                {fmt(econ.llm_request_count)} LLM requests
                            </div>
                        </div>
                        <div className="bg-surface rounded-lg border p-4">
                            <div className="text-xs text-muted mb-1">Avg Cost / Request</div>
                            <div className="text-lg font-bold text-default">{fmtCost(econ.avg_cost_per_request)}</div>
                        </div>
                        <div className="bg-surface rounded-lg border p-4">
                            <div className="text-xs text-muted mb-1">Avg Tokens / Request</div>
                            <div className="text-lg font-bold text-default">{fmt(econ.avg_tokens_per_request)}</div>
                        </div>
                    </div>
                ) : (
                    <div className="bg-surface rounded-lg border p-6 text-center">
                        <DollarSign className="w-8 h-8 text-disabled mx-auto mb-2" />
                        <p className="text-sm text-muted">No LLM token or cost data recorded for this period</p>
                    </div>
                )}
            </div>

            {/* Cost avoided — the reducers' receipts. Two components kept
                apart on purpose: gateway-cache avoidance is an ESTIMATE
                (hits x the window's avg non-cached request cost, basis
                shown) while provider prompt-cache savings are MEASURED
                from the ledger. An honest savings number beats a big one. */}
            {costSavings && (
                <div className="mb-6" data-testid="mc-savings-panel">
                    <h3 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3 flex items-center gap-2">
                        <DollarSign className="w-4 h-4" /> Cost Avoided
                    </h3>
                    <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
                        <div className="bg-surface rounded-lg border p-4">
                            <div className="text-xs text-muted mb-1">Total avoided (period)</div>
                            <div className="text-lg font-bold text-green-700">{fmtCost(costSavings.total_avoided_usd)}</div>
                            <div className="text-xs text-muted mt-1">estimate — components below</div>
                        </div>
                        <div className="bg-surface rounded-lg border p-4">
                            <div className="text-xs text-muted mb-1">Gateway cache (est.)</div>
                            <div className="text-lg font-bold text-default">{fmtCost(costSavings.gateway_cache?.estimated_avoided_usd)}</div>
                            <div className="text-xs text-muted mt-1"
                                title={costSavings.gateway_cache?.basis_avg_cost_per_noncached_request_usd
                                    ? `Basis: ${costSavings.gateway_cache.hits.toLocaleString()} hits × ${fmtCost(costSavings.gateway_cache.basis_avg_cost_per_noncached_request_usd)} avg non-cached request`
                                    : undefined}>
                                {fmt(costSavings.gateway_cache?.hits)} hits &middot; {fmtCompact(costSavings.gateway_cache?.tokens_saved || 0)} tokens saved
                            </div>
                        </div>
                        <div className="bg-surface rounded-lg border p-4">
                            <div className="text-xs text-muted mb-1">Provider prompt cache</div>
                            <div className="text-lg font-bold text-default">{fmtCost(costSavings.provider_prompt_cache?.savings_usd)}</div>
                            <div className="text-xs text-muted mt-1">measured — provider-reported discounts</div>
                        </div>
                        <div className="bg-surface rounded-lg border p-4">
                            <div className="text-xs text-muted mb-1">Cache hit rate</div>
                            <div className="text-lg font-bold text-default">
                                {costSavings.gateway_cache?.hit_rate != null
                                    ? `${(costSavings.gateway_cache.hit_rate * 100).toFixed(1)}%`
                                    : '—'}
                            </div>
                            <div className="text-xs text-muted mt-1">of {fmt(costSavings.llm_requests)} LLM requests</div>
                        </div>
                    </div>
                </div>
            )}

            {/* LLM Media — per-mode billing-unit roll-up. Token
                counters in the LLM Economics block above can't
                represent the work done by image / TTS / STT / video
                routes (which bill in images, chars, seconds), so
                this panel surfaces those typed counters with their
                attributable cost. The cost shown here is also
                included in the LLM Economics row above (records
                share `usage_logs` resource_type='llm'); it's
                duplicated by design for at-a-glance "where did the
                media spend go" attribution. */}
            <MediaUsagePanel data={state.mediaUsage} />
        </>
        );
    };

    // The LLM Economics tab: monthly trend charts (last 12 months) above
    // the period-scoped token/cost counters and the media roll-up.
    const renderEconomics = () => (
        (econLoading && !costMonthly) ? <TabSpinner label="Loading LLM economics…" /> :
        <>
            {/* The export control sits ABOVE the chart grid, not inside it —
                a button dropped into the grid becomes a cell and knocks the
                two charts out of their side-by-side pairing. */}
            <div className="flex justify-end mb-1">
                <CsvButton
                    filename="llm-economics-monthly.csv"
                    headers={['month', 'cost_usd', 'tokens', 'requests', 'unit_cost_per_1k_usd']}
                    rows={(costMonthly?.data || []).map((m: MonthlyCostPoint) => [
                        m.month, m.cost_usd, m.tokens, m.requests, m.unit_cost_per_1k_usd,
                    ])}
                />
            </div>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-6">
                <MonthlyUnitCostChart data={costMonthly?.data || []} />
                <MonthlyRequestsChart data={costMonthly?.data || []} />
            </div>
            {renderLlmEconomics()}
        </>
    );

    const renderMcpA2aOps = () => {
        return (
        <>
            {/* MCP Methods + Top Servers */}
            <h3 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3 flex items-center gap-2">
                <MessageSquare className="w-4 h-4" /> MCP Operations
            </h3>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                {(state.mcpMethods && Object.keys(state.mcpMethods).length > 0) ||
                 (state.mcpToolCalls && Object.keys(state.mcpToolCalls).length > 0) ? (
                    <MCPMethodsChart data={state.mcpMethods || {}} toolCalls={state.mcpToolCalls || undefined} />
                ) : (
                    <div className="bg-surface rounded-lg border p-6">
                        <h4 className="font-medium text-default mb-4">Top MCP Methods</h4>
                        <div className="h-[200px] flex items-center justify-center">
                            <p className="text-sm text-muted">No MCP method data for this period</p>
                        </div>
                    </div>
                )}
                <div className="bg-surface rounded-lg border p-4">
                    <div className="grid grid-cols-2 gap-4">
                        {/* AI Models column */}
                        <div>
                            <h4 className="text-sm font-semibold text-default mb-3 flex items-center gap-2">
                                <Brain className="w-4 h-4 text-muted" /> AI Models
                            </h4>
                            <div className="space-y-2">
                                {state.topLlmModels.slice(0, 8).map((m, i) => {
                                    const max = state.topLlmModels[0]?.request_count || 1;
                                    const pct = (m.request_count / max) * 100;
                                    return (
                                        <div key={m.model_id} className="flex items-center gap-2">
                                            <span className="text-xs text-muted w-4 text-right">{i + 1}</span>
                                            <div className="flex-1 min-h-0 min-w-0">
                                                <div className="flex items-center justify-between mb-0.5">
                                                    <span className="text-xs font-medium text-secondary" title={m.model_id}>
                                                        {m.name} <span className="text-muted">({m.model_name})</span>
                                                    </span>
                                                    <span className="text-xs text-muted flex-shrink-0 ml-2">{fmt(m.request_count)} req</span>
                                                </div>
                                                <div className="w-full bg-surface-raised rounded-full h-1">
                                                    <div className="bg-purple-400 h-1 rounded-full" style={{ width: `${pct}%` }} />
                                                </div>
                                            </div>
                                        </div>
                                    );
                                })}
                                {state.topLlmModels.length === 0 && (
                                    <p className="text-xs text-muted text-center py-4">No AI model activity</p>
                                )}
                            </div>
                        </div>
                        {/* MCP Servers column */}
                        <div>
                            <h4 className="text-sm font-semibold text-default mb-3 flex items-center gap-2">
                                <MCPIcon className="w-4 h-4 text-muted" /> MCP Servers
                            </h4>
                            <div className="space-y-2">
                                {state.topMcpServers.slice(0, 8).map((s, i) => {
                                    const max = state.topMcpServers[0]?.request_count || 1;
                                    const pct = (s.request_count / max) * 100;
                                    return (
                                        <div key={s.server_id} className="flex items-center gap-2">
                                            <span className="text-xs text-muted w-4 text-right">{i + 1}</span>
                                            <div className="flex-1 min-h-0 min-w-0">
                                                <div className="flex items-center justify-between mb-0.5">
                                                    <span className="text-xs font-medium text-secondary" title={s.server_id}>
                                                        {s.server_name || s.server_id}
                                                    </span>
                                                    <span className="text-xs text-muted flex-shrink-0 ml-2">{fmt(s.request_count)} req</span>
                                                </div>
                                                <div className="w-full bg-surface-raised rounded-full h-1">
                                                    <div className="bg-blue-400 h-1 rounded-full" style={{ width: `${pct}%` }} />
                                                </div>
                                            </div>
                                        </div>
                                    );
                                })}
                                {state.topMcpServers.length === 0 && (
                                    <p className="text-xs text-muted text-center py-4">No MCP server activity</p>
                                )}
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            {/* ── A2A Operations ───────────────────────────────────── */}
            {/*
             * Hidden when the period has no A2A traffic — showing the
             * empty section forever would be noise on tenants that
             * don't use Agent-to-Agent today. The Overview tab's KPI
             * row gives visibility that A2A exists even when this
             * section collapses.
             */}
            {((state.a2aOverview && state.a2aOverview.total_requests > 0) ||
              state.topA2aAgents.length > 0 ||
              (state.a2aCapabilities && Object.keys(state.a2aCapabilities).length > 0)) && (
                <div className="mt-6">
                    <h3 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3 flex items-center gap-2">
                        <Share2 className="w-4 h-4" /> A2A Operations
                    </h3>
                    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                        {/* Capabilities table */}
                        {state.a2aCapabilities && Object.keys(state.a2aCapabilities).length > 0 ? (
                            <A2ACapabilitiesChart data={state.a2aCapabilities} />
                        ) : (
                            <div className="bg-surface rounded-lg border p-6">
                                <h4 className="font-medium text-default mb-4">A2A Capabilities</h4>
                                <div className="h-[200px] flex items-center justify-center">
                                    <p className="text-sm text-muted">No A2A capability data for this period</p>
                                </div>
                            </div>
                        )}

                        {/* Top agent cards + headline overview */}
                        <div className="bg-surface rounded-lg border p-4">
                            {state.a2aOverview && state.a2aOverview.total_requests > 0 && (
                                <div className="grid grid-cols-3 gap-3 mb-4 pb-4 border-b border-default">
                                    <div>
                                        <div className="text-xs text-muted">Requests</div>
                                        <div className="text-lg font-bold text-default">
                                            {fmt(state.a2aOverview.total_requests)}
                                        </div>
                                    </div>
                                    <div>
                                        <div className="text-xs text-muted">Success</div>
                                        <div className={`text-lg font-bold ${
                                            state.a2aOverview.success_rate >= 99 ? 'text-green-600' :
                                            state.a2aOverview.success_rate >= 95 ? 'text-yellow-600' :
                                            'text-red-600'
                                        }`}>
                                            {state.a2aOverview.success_rate.toFixed(1)}%
                                        </div>
                                    </div>
                                    <div>
                                        <div className="text-xs text-muted">Avg Latency</div>
                                        <div className="text-lg font-bold text-default">
                                            {fmtDuration(state.a2aOverview.avg_duration_ms)}
                                        </div>
                                    </div>
                                </div>
                            )}
                            <h4 className="text-sm font-semibold text-default mb-3 flex items-center gap-2">
                                <Share2 className="w-4 h-4 text-muted" /> Top Agent Cards
                            </h4>
                            <div className="space-y-2">
                                {state.topA2aAgents.slice(0, 8).map((a, i) => {
                                    const max = state.topA2aAgents[0]?.request_count || 1;
                                    const pct = (a.request_count / max) * 100;
                                    return (
                                        <div key={a.card_id} className="flex items-center gap-2">
                                            <span className="text-xs text-muted w-4 text-right">{i + 1}</span>
                                            <div className="flex-1 min-h-0 min-w-0">
                                                <div className="flex items-center justify-between mb-0.5">
                                                    <span className="text-xs font-medium text-secondary" title={a.card_id}>
                                                        {a.card_name}
                                                    </span>
                                                    <span className="text-xs text-muted flex-shrink-0 ml-2">{fmt(a.request_count)} req</span>
                                                </div>
                                                <div className="w-full bg-surface-raised rounded-full h-1">
                                                    <div className="bg-emerald-400 h-1 rounded-full" style={{ width: `${pct}%` }} />
                                                </div>
                                            </div>
                                        </div>
                                    );
                                })}
                                {state.topA2aAgents.length === 0 && (
                                    <p className="text-xs text-muted text-center py-4">No agent activity</p>
                                )}
                            </div>
                        </div>
                    </div>
                </div>
            )}
        </>
        );
    };

    const renderAnalytics = () => (
        <>
            {state.heatmapData && state.heatmapData.heatmap ? (
                <HeatmapGrid data={state.heatmapData} />
            ) : state.usagePatterns?.requests_by_hour && Object.keys(state.usagePatterns.requests_by_hour).length > 0 ? (
                <UsageHeatmap data={state.usagePatterns.requests_by_hour} />
            ) : (
                <div className="bg-surface rounded-lg border p-6">
                    <h4 className="font-medium text-default mb-4">Usage Heatmap</h4>
                    <div className="h-[180px] flex items-center justify-center">
                        <p className="text-sm text-muted">No heatmap data for this period</p>
                    </div>
                </div>
            )}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-4">
                {state.errors?.top_errors && state.errors.top_errors.length > 0 ? (
                    <ErrorDistributionChart data={state.errors.top_errors} />
                ) : (
                    <div className="bg-surface rounded-lg border p-6">
                        <h4 className="font-medium text-default mb-4">Error Distribution</h4>
                        <div className="h-[200px] flex items-center justify-center text-sm text-muted">No errors in this period</div>
                    </div>
                )}
                <div className="bg-surface rounded-lg border p-4">
                    <h4 className="text-sm font-semibold text-default mb-3 flex items-center gap-2">
                        <Users className="w-4 h-4 text-muted" /> User Type Distribution
                    </h4>
                    {state.usagePatterns?.user_type_distribution &&
                     Object.keys(state.usagePatterns.user_type_distribution).length > 0 ? (
                        <div className="space-y-3">
                            {Object.entries(state.usagePatterns.user_type_distribution).map(([type, count]) => {
                                const total = Object.values(state.usagePatterns!.user_type_distribution).reduce((a, b) => a + b, 0);
                                const pct = total > 0 ? (count / total) * 100 : 0;
                                return (
                                    <div key={type}>
                                        <div className="flex items-center justify-between text-xs mb-1">
                                            <span className="font-medium text-secondary capitalize">{type}</span>
                                            <span className="text-muted">{fmt(count)} ({pct.toFixed(1)}%)</span>
                                        </div>
                                        <div className="w-full bg-surface-raised rounded-full h-1.5">
                                            <div className="bg-purple-500 h-1.5 rounded-full transition-all" style={{ width: `${pct}%` }} />
                                        </div>
                                    </div>
                                );
                            })}
                        </div>
                    ) : (
                        <div className="h-[200px] flex items-center justify-center text-sm text-muted">No user type data</div>
                    )}
                </div>
            </div>
        </>
    );

    /** Route active tab to renderer */
    // ── Cost (FinOps) ──
    /** Δ vs the preceding equal window. Green when spend went DOWN — on a
     *  cost surface, down is the good direction. Rendered only when the
     *  compare window exists (never for all-time) and the change is
     *  non-trivial. "new" marks spend with no previous-window baseline. */
    const DeltaChip: React.FC<{ cost: number; prev?: number | null }> = ({ cost, prev }) => {
        if (prev === undefined || prev === null) return null;
        if (prev === 0) {
            if (cost === 0) return null;
            return (
                <span className="text-[10px] font-semibold px-1 py-0.5 rounded bg-sky-100 text-sky-700"
                    title="No spend in the preceding window — new spend">new</span>
            );
        }
        const pct = ((cost - prev) / prev) * 100;
        if (Math.abs(pct) < 0.05) return null;
        const up = pct > 0;
        return (
            <span
                className={`text-[10px] font-semibold px-1 py-0.5 rounded ${
                    up ? 'bg-red-100 text-red-700' : 'bg-green-100 text-green-700'
                }`}
                title={`Previous window: ${fmtCost(prev)}`}
            >
                {up ? '▲' : '▼'}{Math.abs(pct) >= 100 ? Math.round(Math.abs(pct)) : Math.abs(pct).toFixed(1)}%
            </span>
        );
    };

    /** "Export exactly what's on screen" — one click, RFC 4180, BOM'd
     *  for Excel, no server round-trip. */
    const CsvButton: React.FC<{ filename: string; headers: string[]; rows: Array<Array<string | number | null | undefined>> }> =
        ({ filename, headers, rows }) => (
        <button
            onClick={() => downloadCsv(filename, headers, rows)}
            disabled={rows.length === 0}
            title="Download this table as CSV"
            className="text-muted hover:text-primary disabled:opacity-30"
            data-testid={`csv-${filename}`}
        >
            <Download className="w-3.5 h-3.5" />
        </button>
    );

    const renderCostTopTable = (
        title: string,
        rows: any[],
        labelFn?: (r: { name?: string | null; key?: string | null }) => string,
    ) => (
        <div className="bg-surface rounded-lg border p-4">
            <h4 className="font-medium text-default mb-3 flex items-center justify-between">
                {title}
                <CsvButton
                    filename={`${title.toLowerCase().replace(/[^a-z0-9]+/g, '-')}.csv`}
                    headers={['name', 'cost_usd', 'tokens', 'requests', 'prev_cost_usd']}
                    rows={(rows || []).map((r: any) => [
                        (labelFn ? labelFn(r) : (r.name || r.key)), r.cost, r.tokens, r.requests,
                        r.prev_cost ?? '',
                    ])}
                />
            </h4>
            {rows && rows.length > 0 ? (
                <div className="space-y-1.5">
                    {rows.map((r, i) => {
                        const max = rows[0]?.cost || 1;
                        const pct = (r.cost / max) * 100;
                        const label = labelFn ? labelFn(r) : (r.name || r.key);
                        return (
                            <div key={i} className="flex items-center gap-2">
                                <span className="text-xs text-muted w-4 text-right">{i + 1}</span>
                                <div className="flex-1 min-w-0 min-h-0">
                                    <div className="flex items-center justify-between mb-0.5">
                                        <span className="text-xs font-medium text-secondary truncate" title={label}>
                                            {label}
                                        </span>
                                        <span className="text-xs text-muted flex-shrink-0 ml-2 inline-flex items-center gap-1.5">
                                            <DeltaChip cost={r.cost} prev={r.prev_cost} />
                                            {fmtCost(r.cost)} &middot; {fmt(r.requests)} req
                                        </span>
                                    </div>
                                    <div className="w-full bg-surface-raised rounded-full h-1">
                                        <div className="bg-green-400 h-1 rounded-full" style={{ width: `${pct}%` }} />
                                    </div>
                                </div>
                            </div>
                        );
                    })}
                </div>
            ) : (
                <p className="text-xs text-muted text-center py-4">No data for this period</p>
            )}
        </div>
    );

    const renderCost = () => {
        if (costLoading && !costData) return <TabSpinner label="Loading cost data…" />;
        const src = costData?.by_source;
        const budgets = costBudgets?.budgets || [];
        return (
            <>
                {/* Two-plane hero: Governed inline vs Observed imported */}
                <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-6">
                    <div className="bg-surface rounded-lg border p-4">
                        <div className="text-xs text-muted mb-1">Total Spend ({PLANE_OPTIONS.find(p => p.value === plane)?.label})</div>
                        <div className="text-2xl font-bold text-green-700 flex items-center gap-2">
                            {fmtCost(costData?.totals?.cost)}
                            {costData?.previous && (
                                <DeltaChip cost={costData.totals?.cost ?? 0} prev={costData.previous.totals?.cost} />
                            )}
                        </div>
                        <div className="text-xs text-muted mt-1">
                            {fmt(costData?.totals?.tokens)} tokens &middot; {fmt(costData?.totals?.requests)} req
                            {costData?.previous && (
                                <span title={`Preceding equal window: ${fmtCost(costData.previous.totals?.cost)}`}> &middot; prev {fmtCost(costData.previous.totals?.cost)}</span>
                            )}
                        </div>
                    </div>
                    <div className="bg-surface rounded-lg border p-4">
                        <div className="text-xs text-muted mb-1 flex items-center gap-1"><Shield className="w-3 h-3" /> Governed (inline)</div>
                        <div className="text-2xl font-bold text-default">{fmtCost(src?.governed?.cost)}</div>
                        <div className="text-xs text-muted mt-1">{fmt(src?.governed?.requests)} requests &middot; enforced</div>
                    </div>
                    <div className="bg-surface rounded-lg border p-4">
                        <div className="text-xs text-muted mb-1 flex items-center gap-1"><Download className="w-3 h-3" /> Observed (imported)</div>
                        <div className="text-2xl font-bold text-default">{fmtCost(src?.observed?.cost)}</div>
                        <div className="text-xs text-muted mt-1">{fmt(src?.observed?.requests)} requests &middot; observability only</div>
                    </div>
                </div>

                {/* Cost per completed task — the run ledger's FinOps headline.
                    Only meaningful scoped to one AI System: tasks belong to a
                    system, and a cross-fleet average would blend incomparable
                    workloads into a number nobody can act on. */}
                {singleSystemId && (
                    <RunSignalsSection stats={runStats} loading={runStatsLoading} variant="cost" />
                )}

                {/* Budget burn-down */}
                <h3 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3 flex items-center gap-2">
                    <DollarSign className="w-4 h-4" /> Budget Burn-down
                </h3>
                <div className="bg-surface rounded-lg border p-4 mb-6">
                    {budgets.length > 0 ? (
                        <div className="space-y-3">
                            {budgets.map((b: any, i: number) => (
                                <div key={i}>
                                    <div className="flex items-center justify-between mb-1 text-xs">
                                        <span className="font-medium text-secondary">{b.group_name} <span className="text-muted">({b.period})</span></span>
                                        <span className={b.over_warning ? 'text-red-600 font-semibold' : 'text-muted'}>
                                            {fmtCost(b.spend_usd)} / {fmtCost(b.limit_usd)} &middot; {b.utilization_pct}%
                                            {b.projected_over && <span className="text-red-600"> &middot; projected {fmtCost(b.projected_usd)}</span>}
                                        </span>
                                    </div>
                                    <div className="w-full bg-surface-raised rounded-full h-2">
                                        <div className={`h-2 rounded-full ${b.utilization_pct >= 100 ? 'bg-red-500' : b.over_warning ? 'bg-yellow-500' : 'bg-green-500'}`}
                                            style={{ width: `${Math.min(b.utilization_pct, 100)}%` }} />
                                    </div>
                                </div>
                            ))}
                        </div>
                    ) : (
                        <p className="text-sm text-muted text-center py-2">No resource-group budgets configured. Set budgets on a resource group's Governance tab to track burn-down here.</p>
                    )}
                </div>

                {/* Cost by dimension */}
                <h3 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3 flex items-center gap-2">
                    <DollarSign className="w-4 h-4" /> Cost Attribution
                </h3>
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-6">
                    {renderCostTopTable('By Provider', costData?.by_provider || [])}
                    {renderCostTopTable('By Model', costData?.by_model || [])}
                    {renderCostTopTable(
                        'By Team (Resource Group)',
                        costData?.by_team || [],
                        (r) =>
                            r.name && r.key && r.name !== r.key
                                ? `${r.name} (${r.key})`
                                : r.name || r.key || '',
                    )}
                    {renderCostByUser()}
                </div>

                {/* Composition over time — the trend of the MIX, which a
                    period total can't show. Bands sum to the real total
                    (the tail folds into "other" server-side). */}
                <div className="mb-6">
                    <div className="flex justify-end mb-1">
                        <CsvButton
                            filename="cost-composition-daily.csv"
                            headers={['date', 'key', 'name', 'cost_usd', 'requests']}
                            rows={(costSeries?.data || []).flatMap((d: any) =>
                                Object.entries(d.values).map(([k, v]: [string, any]) => [
                                    d.date, k,
                                    costSeries.keys.find((x: any) => x.key === k)?.name ?? k,
                                    v.cost, v.requests,
                                ]))}
                        />
                    </div>
                    <CostCompositionChart series={costSeries} />
                </div>

                {/* Token economics + media (reused) */}
                {costLoading && !costData && <p className="text-xs text-muted mt-2">Loading cost data…</p>}
            </>
        );
    };

    // "By User" gets its own panel: the generic top-table hides two things
    // operators trip over — the list is top-N truncated, and agent-
    // driven spend carries no user_id at all, so the visible rows never
    // sum to the total. A search box + scroll shows every attributed
    // user, and the residual row makes the unattributed remainder explicit
    // instead of leaving the arithmetic silently short.
    const renderCostByUser = () => {
        const rows: Array<{ key?: string; name?: string; cost: number; requests: number }> =
            costData?.by_user || [];
        const totalCost: number = costData?.totals?.cost || 0;
        const totalReqs: number = costData?.totals?.requests || 0;
        const attributedCost = rows.reduce((a, r) => a + (r.cost || 0), 0);
        const attributedReqs = rows.reduce((a, r) => a + (r.requests || 0), 0);
        const residualCost = Math.max(0, totalCost - attributedCost);
        const residualReqs = Math.max(0, totalReqs - attributedReqs);
        const q = costUserFilter.trim().toLowerCase();
        const filtered = q
            ? rows.filter(r => (r.name || r.key || '').toLowerCase().includes(q))
            : rows;
        const max = rows[0]?.cost || 1;
        return (
            <div className="bg-surface rounded-lg border p-4">
                <div className="flex items-center justify-between mb-3 gap-2">
                    <h4 className="font-medium text-default">By User</h4>
                    <input
                        type="text"
                        value={costUserFilter}
                        onChange={e => setCostUserFilter(e.target.value)}
                        placeholder="Search users…"
                        className="text-xs border rounded-md px-2 py-1 w-36 bg-surface text-default placeholder:text-muted"
                    />
                </div>
                {rows.length > 0 ? (
                    <>
                        <div className="space-y-1.5 max-h-64 overflow-y-auto pr-1">
                            {filtered.map((r, i) => {
                                const pct = ((r.cost || 0) / max) * 100;
                                const label = r.name || r.key || '';
                                return (
                                    <div key={i} className="flex items-center gap-2">
                                        <span className="text-xs text-muted w-4 text-right">{rows.indexOf(r) + 1}</span>
                                        <div className="flex-1 min-w-0 min-h-0">
                                            <div className="flex items-center justify-between mb-0.5">
                                                <span className="text-xs font-medium text-secondary truncate" title={label}>
                                                    {label}
                                                </span>
                                                <span className="text-xs text-muted flex-shrink-0 ml-2">
                                                    {fmtCost(r.cost)} &middot; {fmt(r.requests)} req
                                                </span>
                                            </div>
                                            <div className="w-full bg-surface-raised rounded-full h-1">
                                                <div className="bg-green-400 h-1 rounded-full" style={{ width: `${pct}%` }} />
                                            </div>
                                        </div>
                                    </div>
                                );
                            })}
                            {filtered.length === 0 && (
                                <p className="text-xs text-muted py-1">No users match &ldquo;{costUserFilter}&rdquo;</p>
                            )}
                        </div>
                        {residualCost > 0.005 && !q && (
                            <div className="flex items-center justify-between mt-2 pt-2 border-t">
                                <span className="text-xs text-muted italic">
                                    Agents &amp; unattributed (no user)
                                </span>
                                <span className="text-xs text-muted flex-shrink-0 ml-2">
                                    {fmtCost(residualCost)} &middot; {fmt(residualReqs)} req
                                </span>
                            </div>
                        )}
                    </>
                ) : (
                    <p className="text-sm text-muted text-center py-2">No user-attributed spend this period</p>
                )}
            </div>
        );
    };

    // In-tab loading spinner: shown while a tab's own data is fetching
    // and it has nothing to render yet. Once data exists, background
    // refreshes update in place (last-good stays visible) — the spinner
    // never replaces content the operator is already reading.
    const TabSpinner = ({ label }: { label: string }) => (
        <div className="flex items-center justify-center py-16">
            <div className="text-center">
                <RefreshCw className="w-6 h-6 animate-spin text-primary mx-auto mb-3" />
                <p className="text-sm text-muted">{label}</p>
            </div>
        </div>
    );

    // ── Governance posture ──
    const renderGovernance = () => {
        if (govLoading && !governanceData) return <TabSpinner label="Loading governance posture…" />;
        const g = governanceData?.guardrails;
        const s = governanceData?.shadow_ai;
        const breaches = state.usageAlerts || [];
        return (
            <>
                {/* Enforcement outcomes */}
                <h3 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3 flex items-center gap-2">
                    <Shield className="w-4 h-4" /> Enforcement Outcomes (governed inline)
                </h3>
                <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
                    <div className="bg-surface rounded-lg border p-4">
                        <div className="text-xs text-muted mb-1">Guardrail Blocks</div>
                        <div className="text-2xl font-bold text-red-600">{fmt(g?.blocked)}</div>
                        <div className="text-xs text-muted mt-1">{g?.block_rate ?? 0}% of requests</div>
                    </div>
                    <div className="bg-surface rounded-lg border p-4">
                        <div className="text-xs text-muted mb-1">Policy Denials</div>
                        <div className="text-2xl font-bold text-orange-600">{fmt(g?.policy_denied)}</div>
                        <div className="text-xs text-muted mt-1">argument policies</div>
                    </div>
                    <div className="bg-surface rounded-lg border p-4">
                        <div className="text-xs text-muted mb-1">PII Detected</div>
                        <div className="text-2xl font-bold text-yellow-600">{fmt(g?.pii_detected)}</div>
                        <div className="text-xs text-muted mt-1">requests flagged</div>
                    </div>
                    <div className="bg-surface rounded-lg border p-4">
                        <div className="text-xs text-muted mb-1">Inspected Requests</div>
                        <div className="text-2xl font-bold text-default">{fmt(g?.total_requests)}</div>
                        <div className="text-xs text-muted mt-1">block-only granularity</div>
                    </div>
                </div>

                {/* Shadow-AI */}
                <h3 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3 flex items-center gap-2">
                    <AlertTriangle className="w-4 h-4" /> Shadow AI / Un-approved Models
                </h3>
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-6">
                    <div className="bg-surface rounded-lg border p-4">
                        <div className="grid grid-cols-2 gap-4 mb-3">
                            <div>
                                <div className="text-xs text-muted mb-1">Observed (ungoverned) spend</div>
                                <div className="text-lg font-bold text-default">{fmtCost(s?.observed_cost)}</div>
                                <div className="text-xs text-muted">{fmt(s?.observed_requests)} imported requests</div>
                            </div>
                            <div>
                                <div className="text-xs text-muted mb-1">Un-approved model requests</div>
                                <div className="text-lg font-bold text-default">{fmt(s?.unapproved_requests)}</div>
                                <div className="text-xs text-muted">imported / disabled / non-catalog</div>
                            </div>
                        </div>
                        <p className="text-xs text-muted">Traffic the gateway never enforced — surfaced via Traffic Data Import for detection.</p>
                    </div>
                    {renderCostTopTable('Top Un-approved Models', (s?.unapproved_models || []).map((m: any) => ({ key: m.model, name: m.model, cost: m.cost, requests: m.requests })))}
                </div>

                {/* Sections contributed to the `mission-control.governance-sections`
                    slot (Discovery Coverage is). */}
                {governanceSections.map((item) => (
                    <React.Fragment key={item.id}>{item.render({ posture: governanceData as unknown, onChanged: refreshGovernance })}</React.Fragment>
                ))}

                {/* Compliance coverage (reused endpoint) */}
                <h3 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3 flex items-center gap-2">
                    <CheckCircle className="w-4 h-4" /> Compliance Coverage
                </h3>
                <div className="grid grid-cols-2 lg:grid-cols-5 gap-4 mb-6">
                    {complianceCards.length > 0 ? complianceCards.map((c: any) => (
                        <div key={c.framework} className="bg-surface rounded-lg border p-4">
                            <div className="text-xs text-muted mb-1 uppercase">{c.framework.replace(/_/g, ' ')}</div>
                            <div className="text-lg font-bold text-default">{fmt(c.total_tagged_rows)}</div>
                            <div className="text-xs text-muted mt-1">{c.declared ? 'tagged rows' : 'not enabled'}</div>
                        </div>
                    )) : (
                        <div className="col-span-full bg-surface rounded-lg border p-4 text-center text-sm text-muted">
                            No compliance frameworks enabled for this tenant.
                        </div>
                    )}
                </div>

                {/* Budget / quota breaches (reused usage-alerts feed) */}
                <h3 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3 flex items-center gap-2">
                    <AlertTriangle className="w-4 h-4" /> Budget &amp; Quota Breaches
                </h3>
                <div className="bg-surface rounded-lg border p-4">
                    {breaches.length > 0 ? (
                        <div className="space-y-2">
                            {breaches.slice(0, 10).map((a: any) => (
                                <div key={a.id} className="flex items-center justify-between text-xs border-b border-default last:border-0 pb-1.5">
                                    <span className="text-secondary">{a.group_name || a.group_id} &middot; {a.metric}</span>
                                    <span className={a.alert_type === 'hard_stop' ? 'text-red-600 font-semibold' : 'text-yellow-600'}>
                                        {a.alert_type} &middot; {a.utilization_pct ?? a.threshold_pct}%
                                    </span>
                                </div>
                            ))}
                        </div>
                    ) : (
                        <p className="text-sm text-muted text-center py-2">No active budget or quota breaches.</p>
                    )}
                </div>
                {govLoading && !governanceData && <p className="text-xs text-muted mt-2">Loading governance data…</p>}
            </>
        );
    };

    // ── Usage (traffic + analytics + MCP/A2A) ──
    const renderUsage = () => (
        <>
            {renderTraffic()}
            <div className="mt-6">{renderMcpA2aOps()}</div>
            <div className="mt-6">{renderAnalytics()}</div>
        </>
    );

    // ── Reliability (performance + infrastructure + cache) ──
    const renderReliability = () => (
        <>
            {renderPerformance()}
            <div className="mt-6">{renderInfrastructure()}</div>
        </>
    );


    // ── Agents (per-agent runtime authorization) ──
    const renderAgents = () => {
        if (agentLoading && !agentActivity) return <TabSpinner label="Loading agent activity…" />;
        const a = agentActivity;
        if (agentLoading && !a) {
            return (
                <div className="flex items-center justify-center py-16 text-muted">
                    <Loader2 className="w-5 h-5 animate-spin mr-2" /> Loading agent activity…
                </div>
            );
        }
        const top = a?.top_agents || [];
        const denied = a?.recent_denied || [];
        return (
            <>
                <h3 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3 flex items-center gap-2">
                    <Bot className="w-4 h-4" /> Agent Actions (per-agent runtime authorization)
                </h3>
                <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
                    <div className="bg-surface rounded-lg border p-4">
                        <div className="text-xs text-muted mb-1">Agent Actions</div>
                        <div className="text-2xl font-bold">{fmt(a?.total_actions)}</div>
                        <div className="text-xs text-muted mt-1">governed agent-driven calls</div>
                    </div>
                    <div className="bg-surface rounded-lg border p-4">
                        <div className="text-xs text-muted mb-1">Allowed</div>
                        <div className="text-2xl font-bold text-green-600">{fmt(a?.allowed)}</div>
                        <div className="text-xs text-muted mt-1">passed agent authz</div>
                    </div>
                    <div className="bg-surface rounded-lg border p-4">
                        <div className="text-xs text-muted mb-1">Approval Required</div>
                        <div className="text-2xl font-bold text-yellow-600">{fmt(a?.approval_required)}</div>
                        <div className="text-xs text-muted mt-1">{a?.approval_rate ?? 0}% of actions</div>
                    </div>
                    <div className="bg-surface rounded-lg border p-4">
                        <div className="text-xs text-muted mb-1">Denied</div>
                        <div className="text-2xl font-bold text-red-600">{fmt(a?.denied)}</div>
                        <div className="text-xs text-muted mt-1">{a?.deny_rate ?? 0}% of actions</div>
                    </div>
                </div>

                {/* Top agents */}
                <h3 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3">Top Agents</h3>
                <div className="bg-surface rounded-lg border mb-6 overflow-hidden">
                    <table className="w-full text-sm">
                        <thead className={TABLE_THEAD}>
                            <tr>
                                <th className={`text-left px-4 py-2 ${TABLE_TH_TYPE}`}>Agent</th>
                                <th className={`text-right px-4 py-2 ${TABLE_TH_TYPE}`}>Actions</th>
                                <th className={`text-right px-4 py-2 ${TABLE_TH_TYPE}`}>Approval</th>
                                <th className={`text-right px-4 py-2 ${TABLE_TH_TYPE}`}>Denied</th>
                                <th className={`text-right px-4 py-2 ${TABLE_TH_TYPE}`}>Deny Rate</th>
                            </tr>
                        </thead>
                        <tbody className={TABLE_TBODY}>
                            {top.length === 0 && (
                                <tr><td colSpan={5} className="px-4 py-6 text-center text-muted">No agent activity in this period.</td></tr>
                            )}
                            {top.map((row: any) => (
                                <tr key={row.agent_id}>
                                    <td className="px-4 py-2 font-mono text-xs">{row.agent_id}</td>
                                    <td className="px-4 py-2 text-right">{fmt(row.calls)}</td>
                                    <td className="px-4 py-2 text-right text-yellow-600">{fmt(row.approval_required)}</td>
                                    <td className="px-4 py-2 text-right text-red-600">{fmt(row.denied)}</td>
                                    <td className="px-4 py-2 text-right">{row.deny_rate}%</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>

                {/* Recent denied actions */}
                <h3 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3">Recent Denied Actions</h3>
                <div className="bg-surface rounded-lg border overflow-hidden">
                    <table className="w-full text-sm">
                        <thead className={TABLE_THEAD}>
                            <tr>
                                <th className={`text-left px-4 py-2 ${TABLE_TH_TYPE}`}>Agent</th>
                                <th className={`text-left px-4 py-2 ${TABLE_TH_TYPE}`}>Surface</th>
                                <th className={`text-left px-4 py-2 ${TABLE_TH_TYPE}`}>Capability</th>
                                <th className={`text-left px-4 py-2 ${TABLE_TH_TYPE}`}>Grant</th>
                                <th className={`text-left px-4 py-2 ${TABLE_TH_TYPE}`}>When</th>
                            </tr>
                        </thead>
                        <tbody className={TABLE_TBODY}>
                            {denied.length === 0 && (
                                <tr><td colSpan={5} className="px-4 py-6 text-center text-muted">No denied actions in this period.</td></tr>
                            )}
                            {denied.map((row: any, i: number) => (
                                <tr key={i}>
                                    <td className="px-4 py-2 font-mono text-xs">{row.agent_id}</td>
                                    <td className="px-4 py-2">{row.proxy_type}</td>
                                    <td className="px-4 py-2">{row.capability || '—'}</td>
                                    <td className="px-4 py-2 font-mono text-xs">{row.grant_id || '—'}</td>
                                    <td className="px-4 py-2 text-muted text-xs">{row.timestamp ? new Date(row.timestamp).toLocaleString() : '—'}</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>

                {/* Semantic policy shadow verdicts — measure before enforcing */}
                {a?.semantic_policy && (
                    <div className="mt-6">
                        <h3 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3">
                            Semantic Policy (intent governance)
                        </h3>
                        <div className="grid grid-cols-2 lg:grid-cols-2 gap-4 mb-3 max-w-md">
                            <div className="bg-surface rounded-lg border p-4">
                                <div className="text-xs text-muted mb-1">Enforced blocks</div>
                                <div className="text-2xl font-bold text-red-600">{fmt(a.semantic_policy.blocked)}</div>
                            </div>
                            <div className="bg-surface rounded-lg border p-4">
                                <div className="text-xs text-muted mb-1">Shadow would-blocks</div>
                                <div className="text-2xl font-bold text-yellow-600">{fmt(a.semantic_policy.shadow_would_block)}</div>
                                <div className="text-xs text-muted mt-1">measure, then flip to enforce</div>
                            </div>
                        </div>
                        {(a.semantic_policy.recent_shadow || []).length > 0 && (
                            <div className="bg-surface rounded-lg border overflow-hidden">
                                <table className="w-full text-sm">
                                    <thead className={TABLE_THEAD}>
                                        <tr>
                                            <th className={`text-left px-4 py-2 ${TABLE_TH_TYPE}`}>Policy</th>
                                            <th className={`text-left px-4 py-2 ${TABLE_TH_TYPE}`}>Surface</th>
                                            <th className={`text-left px-4 py-2 ${TABLE_TH_TYPE}`}>Reason</th>
                                            <th className={`text-left px-4 py-2 ${TABLE_TH_TYPE}`}>When</th>
                                        </tr>
                                    </thead>
                                    <tbody className={TABLE_TBODY}>
                                        {a.semantic_policy.recent_shadow.map((s: any, i: number) => (
                                            <tr key={i}>
                                                <td className="px-4 py-2 font-mono text-xs">{s.policy_id}</td>
                                                <td className="px-4 py-2 text-xs">{s.proxy_type}</td>
                                                <td className="px-4 py-2 text-xs text-muted">{s.reason || '—'}</td>
                                                <td className="px-4 py-2 text-muted text-xs">{s.timestamp ? new Date(s.timestamp).toLocaleString() : '—'}</td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        )}
                    </div>
                )}
            </>
        );
    };

    const renderActivePanel = () => {
        switch (activeTab) {
            case 'cost':            return renderCost();
            case 'economics':       return renderEconomics();
            case 'governance':      return renderGovernance();
            case 'agents':          return renderAgents();
            case 'usage':           return renderUsage();
            case 'reliability':     return renderReliability();
            default:
                return contributed.get(activeTab)?.render({ hours: periodToHours(selectedPeriod) }) ?? renderUsage();
        }
    };


    // ============================================================================
    // Render
    // ============================================================================

    return (
        <div className={`app-column h-full ${className}`}>
            {/* ── HEADER ── */}
            <div className="flex-none px-6 pt-6 pb-3">
                <div className="flex items-center justify-between">
                    <div>
                        <h1 className="text-xl font-bold text-default">Analytics</h1>
                        <p className="text-sm text-muted">
                            Deep-dive metrics — traffic, cost, policy posture &amp; reliability across governed and observed AI workloads
                        </p>
                    </div>
                    <div className="flex items-center gap-3">
                        {/* AI System (Resource Group) scope — applies only to the
                            usage_logs / proxy_logs-backed tabs (Cost / Governance /
                            Agents); disabled elsewhere since proxy_metrics has no
                            per-group attribution. AI Systems are RGs. */}
                        {(() => {
                            const enabled = GROUP_FILTERABLE_TABS.has(activeTab) && groupFilterOptions.length > 0;
                            const title = groupFilterOptions.length === 0
                                ? 'No AI Systems defined yet — create a Resource Group with type "AI System" under Resource Groups'
                                : enabled
                                    ? 'Scope to one or several AI Systems (a team\'s portfolio is a FinOps unit too)'
                                    : 'AI System scoping applies to the Overview, Cost, Economics, Governance & Agents tabs';
                            const label = selectedSystemIds.length === 0
                                ? 'All AI Systems'
                                : selectedSystemIds.length === 1
                                    ? (groupFilterOptions.find((o) => o.id === selectedSystemIds[0])?.name
                                        ?? selectedSystemIds[0])
                                    : `${selectedSystemIds.length} AI Systems`;
                            const toggle = (id: string) => {
                                const next = selectedSystemIds.includes(id)
                                    ? selectedSystemIds.filter((x) => x !== id)
                                    : [...selectedSystemIds, id];
                                setGroupFilterId(next.length === 0 ? 'all' : next.join(','));
                            };
                            return (
                                <div className="relative">
                                    <button
                                        onClick={() => setSystemPickerOpen((o) => !o)}
                                        disabled={!enabled}
                                        title={title}
                                        aria-label="Scope to AI Systems"
                                        data-testid="mc-system-picker"
                                        className={`px-3 py-1.5 text-xs font-medium rounded-lg bg-surface-raised border border-strong text-default flex items-center gap-1.5 ${!enabled ? 'opacity-50 cursor-not-allowed' : 'hover:border-primary'}`}
                                    >
                                        {label}
                                        <span className="text-muted">▾</span>
                                    </button>
                                    {systemPickerOpen && enabled && (
                                        <div className="absolute left-0 top-full mt-1 z-20 min-w-[240px] bg-surface border border-default rounded-lg shadow-lg p-2"
                                            data-testid="mc-system-picker-popover">
                                            <button
                                                onClick={() => { setGroupFilterId('all'); setSystemPickerOpen(false); }}
                                                className={`w-full text-left px-2 py-1.5 text-xs rounded hover:bg-surface-raised ${selectedSystemIds.length === 0 ? 'font-semibold text-primary' : 'text-secondary'}`}
                                            >
                                                All AI Systems
                                            </button>
                                            <div className="my-1 border-t border-default" />
                                            {groupFilterOptions.map((s) => (
                                                <label key={s.id}
                                                    className="flex items-center gap-2 px-2 py-1.5 text-xs rounded hover:bg-surface-raised cursor-pointer text-default">
                                                    <input
                                                        type="checkbox"
                                                        checked={selectedSystemIds.includes(s.id)}
                                                        onChange={() => toggle(s.id)}
                                                        className="rounded border-strong text-primary focus:ring-primary"
                                                    />
                                                    <span className="truncate">{s.name}</span>
                                                </label>
                                            ))}
                                        </div>
                                    )}
                                </div>
                            );
                        })()}

                        {/* Plane selector — Governed / Observed / All */}
                        <div className="flex bg-surface-raised rounded-lg p-0.5" title="Filter by provenance plane">
                            {PLANE_OPTIONS.map(({ value, label, title }) => (
                                <button key={value} onClick={() => setPlane(value)} title={title}
                                    className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
                                        plane === value
                                            ? 'bg-surface text-default shadow-sm'
                                            : 'text-muted hover:text-secondary'
                                    }`}>{label}</button>
                            ))}
                        </div>

                        {/* Period selector */}
                        <div className="flex bg-surface-raised rounded-lg p-0.5">
                            {PERIOD_OPTIONS.map(({ value, label, title }) => (
                                <button key={value} onClick={() => { setSelectedPeriod(value); setCustomOpen(false); }}
                                    title={title}
                                    className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
                                        selectedPeriod === value
                                            ? 'bg-surface text-default shadow-sm'
                                            : 'text-muted hover:text-secondary'
                                    }`}>{label}</button>
                            ))}
                            {/* Custom calendar range — the FinOps "show me March"
                                / "the incident week" ask. End date inclusive. */}
                            <div className="relative">
                                <button
                                    onClick={() => setCustomOpen(o => !o)}
                                    title="Custom calendar date range"
                                    data-testid="mc-custom-period"
                                    className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
                                        selectedPeriod.startsWith('custom:')
                                            ? 'bg-surface text-default shadow-sm'
                                            : 'text-muted hover:text-secondary'
                                    }`}>
                                    {selectedPeriod.startsWith('custom:')
                                        ? selectedPeriod.slice(7).replace(':', ' → ')
                                        : 'Custom'}
                                </button>
                                {customOpen && (
                                    <div className="absolute right-0 top-full mt-1 z-20 bg-surface border border-default rounded-lg shadow-lg p-3 flex items-end gap-2"
                                        data-testid="mc-custom-popover">
                                        <label className="text-xs text-muted">
                                            From
                                            <input type="date" value={customStart}
                                                onChange={e => setCustomStart(e.target.value)}
                                                className="block mt-0.5 text-xs border border-default rounded px-1.5 py-1 bg-surface text-default" />
                                        </label>
                                        <label className="text-xs text-muted">
                                            To
                                            <input type="date" value={customEnd}
                                                onChange={e => setCustomEnd(e.target.value)}
                                                className="block mt-0.5 text-xs border border-default rounded px-1.5 py-1 bg-surface text-default" />
                                        </label>
                                        <button
                                            disabled={!customStart || !customEnd || customEnd < customStart}
                                            onClick={() => {
                                                setSelectedPeriod(`custom:${customStart}:${customEnd}`);
                                                setCustomOpen(false);
                                            }}
                                            className="px-2.5 py-1 text-xs font-medium rounded bg-primary text-white disabled:opacity-40">
                                            Apply
                                        </button>
                                    </div>
                                )}
                            </div>
                        </div>

                        {/* Auto-refresh */}
                        <label className="flex items-center gap-1.5 text-xs text-muted cursor-pointer">
                            <input type="checkbox" checked={autoRefresh} onChange={e => setAutoRefresh(e.target.checked)}
                                className="rounded border-strong text-primary focus:ring-primary" />
                            <span>Auto</span>
                        </label>

                        {/* Refresh */}
                        <button onClick={() => { runBackground(loadAllData(true), 'load all data'); setRefreshTick(t => t + 1); }} disabled={refreshing}
                            className="flex items-center gap-1.5 px-3 py-1.5 text-xs text-secondary bg-surface border border-default rounded-lg hover:bg-surface-alt disabled:opacity-50 transition-colors">
                            <RefreshCw className={`w-3.5 h-3.5 ${refreshing ? 'animate-spin' : ''}`} />
                            Refresh
                        </button>

                        {/* Export menu */}
                        <div className="relative">
                            <button onClick={() => setExportMenuOpen((o) => !o)}
                                data-testid="mc-export-menu"
                                className="flex items-center gap-1.5 px-3 py-1.5 text-xs text-white bg-primary-500 hover:bg-primary-600 rounded-lg transition-colors">
                                <Download className="w-3.5 h-3.5" /> Export <span>▾</span>
                            </button>
                            {exportMenuOpen && (
                                <div className="absolute right-0 top-full mt-1 z-20 min-w-[230px] bg-surface border border-default rounded-lg shadow-lg py-1 text-xs">
                                    <button onClick={() => { setExportMenuOpen(false); runBackground(handleExport(), 'export'); }}
                                        className="w-full text-left px-3 py-2 hover:bg-surface-raised text-default">
                                        Metrics JSON
                                        <span className="block text-muted">Raw metrics payload for this period</span>
                                    </button>
                                    <button onClick={() => { setExportMenuOpen(false); runBackground(handleExportFocus(), 'export focus'); }}
                                        className="w-full text-left px-3 py-2 hover:bg-surface-raised text-default">
                                        FOCUS CSV (FinOps)
                                        <span className="block text-muted">Daily cost rows, FinOps Open Cost &amp; Usage spec</span>
                                    </button>
                                    <button onClick={() => { setExportMenuOpen(false); runBackground(handleExportStatement(), 'export statement'); }}
                                        disabled={!singleSystemId}
                                        className="w-full text-left px-3 py-2 hover:bg-surface-raised text-default disabled:opacity-40"
                                        title={singleSystemId ? undefined : 'Select exactly one AI System'}>
                                        Monthly statement (HTML)
                                        <span className="block text-muted">One system, one month — cost, tasks, health</span>
                                    </button>
                                </div>
                            )}
                        </div>
                    </div>
                </div>

                {/* Status bar — live real-time metrics + connection info */}
                <div className="flex items-center justify-between mt-2 text-xs text-muted">
                    <div className="flex items-center gap-3">
                        <span>Updated: {lastUpdate?.toLocaleTimeString() || 'never'}</span>
                        {autoRefresh && <span className="text-disabled">{'\u2022'} auto-refresh 30s</span>}
                    </div>
                    <div className="flex items-center gap-4">
                        {/* Live metrics */}
                        {state.realTime && (
                            <div className="flex items-center gap-3 text-muted border-r border-default pr-4 mr-1">
                                <span className="flex items-center gap-1">
                                    <Activity className="w-3 h-3 text-blue-500" />
                                    <span className="font-medium">{fmt(state.realTime.requests_per_minute)}</span> req/min
                                </span>
                                <span className="flex items-center gap-1">
                                    <CheckCircle className="w-3 h-3 text-green-500" />
                                    <span className="font-medium">{state.realTime.current_success_rate.toFixed(1)}%</span>
                                </span>
                                <span className="flex items-center gap-1">
                                    <Users className="w-3 h-3 text-purple-500" />
                                    <span className="font-medium">{fmt(state.realTime.active_sessions)}</span> sessions
                                </span>
                            </div>
                        )}
                        {/* Connection status */}
                        <div className="flex items-center gap-1.5">
                            <div className={`w-1.5 h-1.5 rounded-full ${
                                refreshing ? 'bg-yellow-400 animate-pulse' : error ? 'bg-red-400' : 'bg-green-400'
                            }`} />
                            <span>{refreshing ? 'Refreshing...' : error ? 'Error' : 'Connected'}</span>
                        </div>
                    </div>
                </div>
            </div>

            {/* ── ASSURANCE BAND — the selected AI System's identity and
                 lifecycle verdicts (health / liveness / drift / coverage),
                 pinned above the tabs so no tab can hide them. ── */}
            {singleSystemId && (
                <div className="flex-none px-6 pt-3">
                    <AssuranceBand
                        systemId={singleSystemId}
                        system={fleet?.systems.find((s) => s.id === singleSystemId) ?? null}
                        stats={runStats}
                        reloadKey={refreshTick}
                        onOpenAiSystems={setAppTab ? () => setAppTab('ai-systems') : undefined}
                    />
                </div>
            )}

            {/* ── STICKY TAB BAR ── */}
            <div className="flex-none px-6 sticky top-0 z-10 bg-surface border-b border-default">
                <nav className="flex gap-1 -mb-px" aria-label="Mission Control tabs">
                    {tabs.map(tab => {
                        const isActive = activeTab === tab.id;
                        const infraBadge = tab.id === 'reliability' && totalResources > 0;

                        return (
                            <button key={tab.id} onClick={() => setActiveTab(tab.id)}
                                className={`
                                    flex items-center gap-1.5 px-3 py-2.5 text-xs font-medium
                                    border-b-2 transition-colors whitespace-nowrap
                                    ${isActive
                                        ? 'border-primary text-primary'
                                        : 'border-transparent text-muted hover:text-secondary hover:border-strong'
                                    }
                                `}
                                aria-selected={isActive} role="tab">
                                <span className={isActive ? 'text-primary' : 'text-muted'}>{tab.icon}</span>
                                {tab.label}
                                {infraBadge && (
                                    <span className={`ml-1 px-1.5 py-0.5 text-[10px] font-bold rounded-full leading-none ${
                                        systemStatus === 'healthy' ? 'bg-green-100 text-green-700' : 'bg-yellow-100 text-yellow-700'
                                    }`}>{healthyResources}/{totalResources}</span>
                                )}
                            </button>
                        );
                    })}
                </nav>
            </div>

            {/* Error banner */}
            {error && (
                <div className="flex-none mx-6 mt-4 bg-red-50 border border-red-200 rounded-lg p-3 flex items-center gap-2">
                    <AlertTriangle className="w-4 h-4 text-red-500 flex-shrink-0" />
                    <span className="text-sm text-red-700 flex-1 min-h-0 min-w-0">{error}</span>
                    <button onClick={() => setError(null)} className="text-red-400 hover:text-red-600">&times;</button>
                </div>
            )}

            {/* ── SCROLLABLE TAB CONTENT ── */}
            <div className="scroll-region px-6 py-6 min-h-0">
                {(['fleet', 'scope', ...(ANALYTICS_TAB_PARTS[activeTab] ?? (contributed.has(activeTab) ? [] : ANALYTICS_TAB_PARTS.usage))] as AnalyticsPart[]).map((part) => (
                    <ReadFailureNotice
                        key={part}
                        failure={readFailure(part)}
                        onRetry={() => { runBackground(loadAllData(true), 'all data'); setRefreshTick((t) => t + 1); }}
                    />
                ))}
                {renderActivePanel()}
            </div>
        </div>
    );
};

export default MissionControl;

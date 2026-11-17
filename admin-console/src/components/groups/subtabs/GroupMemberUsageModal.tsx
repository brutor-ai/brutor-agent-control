/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useEffect, useState, useCallback } from 'react';
import { TABLE_TH_TYPE } from '../../ui/tableStyles';
import { useTimespanPreference } from '../../../hooks/useTimespanPreference';
import {
    X,
    BarChart3,
    Brain,
    Wrench,
    DollarSign,
    Activity,
    Loader,
    AlertCircle,
    RefreshCw,
    Image as ImageIcon,
    Speaker,
    Mic,
    Video,
} from 'lucide-react';
import { useGateway } from '../../../hooks/useGateway';
import {
    GroupUsageResponse,
    UsagePeriod,
    MediaMode,
} from '../../../lib/groupTypes';
import { errorMessage } from '../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';

interface Props {
    /** Resource group the member belongs to. */
    groupId: string;
    groupName: string;
    /** Gateway end-user id. The Members tab carries it as a
     *  string, but `usage_logs.user_id` is integer — we parse
     *  inside the fetch. */
    userId: string;
    username: string;
    displayName?: string;
    onClose: () => void;
}

const PERIOD_OPTIONS: { value: UsagePeriod; label: string }[] = [
    { value: '24h', label: '24h' },
    { value: '7d', label: '7d' },
    { value: '30d', label: '30d' },
    { value: '90d', label: '90d' },
    { value: 'all', label: 'All' },
];

const PERIOD_VALUES = PERIOD_OPTIONS.map((o) => o.value);

// ---------- Format helpers (mirror GroupUsageTab so the two
//            views read consistently) ----------

const fmt = (n: number): string => n.toLocaleString();

const fmtCost = (n: number): string => {
    if (!n) return '$0.00';
    if (n < 0.01) return `$${n.toFixed(6)}`;
    if (n < 1) return `$${n.toFixed(4)}`;
    return `$${n.toFixed(2)}`;
};

const fmtMs = (ms: number): string => {
    if (!ms) return '0ms';
    if (ms < 1000) return `${Math.round(ms)}ms`;
    return `${(ms / 1000).toFixed(1)}s`;
};

const MEDIA_MODE_META: Record<MediaMode, { label: string; Icon: React.FC<{ className?: string }> }> = {
    image_generation: { label: 'Image', Icon: ImageIcon },
    audio_speech: { label: 'TTS', Icon: Speaker },
    audio_transcription: { label: 'Transcription', Icon: Mic },
    video_generation: { label: 'Video', Icon: Video },
};

const SummaryCard: React.FC<{
    label: string;
    value: string;
    sublabel?: string;
    Icon: React.FC<{ className?: string }>;
    tone?: 'default' | 'cost' | 'error';
}> = ({ label, value, sublabel, Icon, tone = 'default' }) => {
    const toneClass =
        tone === 'cost'
            ? 'text-secondary'
            : tone === 'error'
              ? 'text-red-700'
              : 'text-default';
    return (
        <div className="bg-surface border border-default rounded-lg p-3">
            <div className="flex items-center gap-1.5 text-xs text-muted mb-1">
                <Icon className="w-3.5 h-3.5" />
                <span>{label}</span>
            </div>
            <div className={`text-xl font-semibold tabular-nums ${toneClass}`}>{value}</div>
            {sublabel && <div className="text-xs text-muted mt-0.5">{sublabel}</div>}
        </div>
    );
};

const GroupMemberUsageModal: React.FC<Props> = ({
    groupId,
    groupName,
    userId,
    username,
    displayName,
    onClose,
}) => {
    const { fetchGroupUsage } = useGateway();

    // Same shared preference as the usage tab this modal opens over — an
    // operator who set 90d on the group should not land on 30d in the
    // per-member drilldown of that same group.
    const [period, setPeriod] = useTimespanPreference(PERIOD_VALUES, '30d');
    const [data, setData] = useState<GroupUsageResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    const load = useCallback(async () => {
        // Skip the call if the parsed id is invalid — usage_logs.user_id
        // is an Integer FK so non-numeric strings would 422 server-side
        // and surface as a noisy red banner. Show the empty-state copy
        // instead.
        const parsed = Number(userId);
        if (!Number.isFinite(parsed) || parsed <= 0) {
            setData(null);
            setLoading(false);
            setError('User id is not numeric — usage rollup unavailable.');
            return;
        }
        try {
            setLoading(true);
            setError(null);
            const resp = await fetchGroupUsage(groupId, {
                period,
                user_id: parsed,
            });
            setData(resp);
        } catch (err) {
            setError(errorMessage(err, 'Failed to load usage'));
        } finally {
            setLoading(false);
        }
    }, [fetchGroupUsage, groupId, period, userId]);

    useEffect(() => {
        runBackground(load(), 'load');
    }, [load]);

    // ESC closes the modal — operator muscle memory across the
    // admin UI's other modals (Send Message, Add Members, etc.).
    useEffect(() => {
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') onClose();
        };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [onClose]);

    const llm = data?.llm;
    const mcp = data?.mcp;
    const a2a = data?.a2a;
    const media = data?.media;
    const summary = data?.summary;

    const hasAnyActivity =
        !!summary && summary.total_requests > 0;

    return (
        <div
            className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4"
            onClick={onClose}
        >
            <div
                className="bg-surface rounded-lg shadow-xl max-w-4xl w-full max-h-[90vh] overflow-hidden flex flex-col"
                onClick={(e) => e.stopPropagation()}
            >
                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-default">
                    <div className="flex items-center gap-3">
                        <div className="w-10 h-10 bg-primary-100 rounded-lg flex items-center justify-center">
                            <BarChart3 className="w-5 h-5 text-primary-600" />
                        </div>
                        <div>
                            <h2 className="text-lg font-semibold text-default">
                                Usage — {displayName || username}
                            </h2>
                            <p className="text-xs text-muted">
                                @{username} · in group {groupName}
                            </p>
                        </div>
                    </div>
                    <button
                        onClick={onClose}
                        className="p-1.5 text-muted hover:text-secondary hover:bg-surface-alt rounded transition-colors"
                        title="Close"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                {/* Period selector + refresh */}
                <div className="flex items-center justify-between px-6 py-3 border-b border-default bg-surface-alt">
                    <div className="inline-flex rounded-lg border border-default bg-surface overflow-hidden">
                        {PERIOD_OPTIONS.map((opt) => (
                            <button
                                key={opt.value}
                                onClick={() => setPeriod(opt.value)}
                                className={`px-3 py-1.5 text-sm transition-colors ${
                                    period === opt.value
                                        ? 'bg-primary-600 text-white'
                                        : 'text-secondary hover:bg-surface-alt'
                                }`}
                            >
                                {opt.label}
                            </button>
                        ))}
                    </div>
                    <button
                        onClick={asyncHandler(load, 'click')}
                        disabled={loading}
                        className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm bg-surface border border-default rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50"
                        title="Reload"
                    >
                        <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
                        <span>Refresh</span>
                    </button>
                </div>

                {/* Body */}
                <div className="scroll-region p-6">
                    {loading ? (
                        <div className="flex items-center justify-center py-16">
                            <div className="text-center">
                                <Loader className="w-8 h-8 text-primary-600 animate-spin mx-auto mb-2" />
                                <p className="text-sm text-secondary">Loading usage…</p>
                            </div>
                        </div>
                    ) : error ? (
                        <div className="flex items-start space-x-3 p-4 bg-surface-alt border border-default rounded-lg">
                            <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
                            <div className="flex-1 min-h-0 min-w-0">
                                <p className="text-sm font-medium text-red-900">Failed to load usage</p>
                                <p className="text-sm text-red-700 mt-1">{error}</p>
                            </div>
                        </div>
                    ) : !hasAnyActivity ? (
                        <div className="text-center py-16 bg-surface-alt rounded-lg border-2 border-dashed border-strong">
                            <BarChart3 className="w-12 h-12 text-muted mx-auto mb-3" />
                            <p className="text-default font-medium">
                                No activity in the last {PERIOD_OPTIONS.find((o) => o.value === period)?.label || period}
                            </p>
                            <p className="text-sm text-muted mt-2">
                                This member hasn't made any requests against this group during the
                                selected period.
                            </p>
                        </div>
                    ) : (
                        <div className="space-y-6">
                            {/* Summary cards */}
                            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                                <SummaryCard
                                    label="Requests"
                                    value={fmt(summary.total_requests)}
                                    Icon={Activity}
                                />
                                <SummaryCard
                                    label="Total cost"
                                    value={fmtCost(summary.total_cost)}
                                    Icon={DollarSign}
                                    tone="cost"
                                />
                                <SummaryCard
                                    label="Errors"
                                    value={fmt(summary.total_errors)}
                                    sublabel={`${summary.error_rate.toFixed(1)}% error rate`}
                                    Icon={AlertCircle}
                                    tone={summary.total_errors > 0 ? 'error' : 'default'}
                                />
                                <SummaryCard
                                    label="Tokens (LLM)"
                                    value={fmt(llm?.total_tokens ?? 0)}
                                    sublabel={
                                        llm && llm.request_count > 0
                                            ? `avg ${fmt(llm.avg_tokens_per_request)}/req`
                                            : undefined
                                    }
                                    Icon={Brain}
                                />
                            </div>

                            {/* LLM panel */}
                            {llm && llm.request_count > 0 && (
                                <section>
                                    <header className="flex items-center gap-2 mb-2">
                                        <Brain className="w-4 h-4 text-muted" />
                                        <h3 className="text-sm font-semibold text-default">
                                            LLM ({fmt(llm.request_count)} request{llm.request_count === 1 ? '' : 's'})
                                        </h3>
                                        <span className="ml-auto text-xs text-muted">
                                            avg {fmtMs(llm.avg_response_time_ms)} · {fmtCost(llm.total_cost)}
                                        </span>
                                    </header>
                                    {llm.top_models.length > 0 && (
                                        <div className="border border-default rounded-lg overflow-hidden">
                                            <table className="min-w-full divide-y divide-default text-sm">
                                                <thead className="bg-surface-alt">
                                                    <tr>
                                                        <th className={`${TABLE_TH_TYPE} px-3 py-2 text-left`}>Model</th>
                                                        <th className={`${TABLE_TH_TYPE} px-3 py-2 text-right`}>Calls</th>
                                                        <th className={`${TABLE_TH_TYPE} px-3 py-2 text-right`}>Tokens</th>
                                                        <th className={`${TABLE_TH_TYPE} px-3 py-2 text-right`}>Cost</th>
                                                    </tr>
                                                </thead>
                                                <tbody className="divide-y divide-default">
                                                    {llm.top_models.slice(0, 8).map((m) => (
                                                        <tr key={`${m.provider ?? 'unknown'}/${m.model_name}`}>
                                                            <td className="px-3 py-2 text-default">
                                                                {m.model_name}
                                                                {m.provider && (
                                                                    <span className="ml-2 text-xs text-muted">{m.provider}</span>
                                                                )}
                                                            </td>
                                                            <td className="px-3 py-2 text-right tabular-nums">{fmt(m.request_count)}</td>
                                                            <td className="px-3 py-2 text-right tabular-nums">{fmt(m.total_tokens)}</td>
                                                            <td className="px-3 py-2 text-right tabular-nums text-secondary">{fmtCost(m.total_cost)}</td>
                                                        </tr>
                                                    ))}
                                                </tbody>
                                            </table>
                                        </div>
                                    )}
                                </section>
                            )}

                            {/* MCP panel */}
                            {mcp && mcp.request_count > 0 && (
                                <section>
                                    <header className="flex items-center gap-2 mb-2">
                                        <Wrench className="w-4 h-4 text-amber-600" />
                                        <h3 className="text-sm font-semibold text-default">
                                            MCP ({fmt(mcp.total_tool_calls)} tool call{mcp.total_tool_calls === 1 ? '' : 's'})
                                        </h3>
                                        <span className="ml-auto text-xs text-muted">
                                            avg {fmtMs(mcp.avg_execution_time_ms)} · {mcp.error_count} error{mcp.error_count === 1 ? '' : 's'}
                                        </span>
                                    </header>
                                    {mcp.top_tools.length > 0 && (
                                        <div className="border border-default rounded-lg overflow-hidden">
                                            <table className="min-w-full divide-y divide-default text-sm">
                                                <thead className="bg-surface-alt">
                                                    <tr>
                                                        <th className={`${TABLE_TH_TYPE} px-3 py-2 text-left`}>Tool</th>
                                                        <th className={`${TABLE_TH_TYPE} px-3 py-2 text-left`}>Server</th>
                                                        <th className={`${TABLE_TH_TYPE} px-3 py-2 text-right`}>Calls</th>
                                                        <th className={`${TABLE_TH_TYPE} px-3 py-2 text-right`}>Errors</th>
                                                    </tr>
                                                </thead>
                                                <tbody className="divide-y divide-default">
                                                    {mcp.top_tools.slice(0, 8).map((t) => (
                                                        <tr key={`${t.server_name}/${t.tool_name}`}>
                                                            <td className="px-3 py-2 text-default">{t.tool_name}</td>
                                                            <td className="px-3 py-2 text-secondary">{t.server_name}</td>
                                                            <td className="px-3 py-2 text-right tabular-nums">{fmt(t.call_count)}</td>
                                                            <td className={`px-3 py-2 text-right tabular-nums ${t.error_count > 0 ? 'text-red-700' : ''}`}>
                                                                {fmt(t.error_count)}
                                                            </td>
                                                        </tr>
                                                    ))}
                                                </tbody>
                                            </table>
                                        </div>
                                    )}
                                </section>
                            )}

                            {/* A2A panel */}
                            {a2a && a2a.request_count > 0 && (
                                <section>
                                    <header className="flex items-center gap-2 mb-2">
                                        <Activity className="w-4 h-4 text-muted" />
                                        <h3 className="text-sm font-semibold text-default">
                                            A2A ({fmt(a2a.request_count)} request{a2a.request_count === 1 ? '' : 's'})
                                        </h3>
                                        <span className="ml-auto text-xs text-muted">
                                            avg {fmtMs(a2a.avg_duration_ms)}
                                        </span>
                                    </header>
                                    {a2a.top_cards.length > 0 && (
                                        <div className="border border-default rounded-lg overflow-hidden">
                                            <table className="min-w-full divide-y divide-default text-sm">
                                                <thead className="bg-surface-alt">
                                                    <tr>
                                                        <th className={`${TABLE_TH_TYPE} px-3 py-2 text-left`}>Agent card</th>
                                                        <th className={`${TABLE_TH_TYPE} px-3 py-2 text-right`}>Calls</th>
                                                        <th className={`${TABLE_TH_TYPE} px-3 py-2 text-right`}>Errors</th>
                                                    </tr>
                                                </thead>
                                                <tbody className="divide-y divide-default">
                                                    {a2a.top_cards.slice(0, 8).map((c) => (
                                                        <tr key={c.card_id}>
                                                            <td className="px-3 py-2 text-default">{c.card_name}</td>
                                                            <td className="px-3 py-2 text-right tabular-nums">{fmt(c.request_count)}</td>
                                                            <td className={`px-3 py-2 text-right tabular-nums ${c.error_count > 0 ? 'text-red-700' : ''}`}>
                                                                {fmt(c.error_count)}
                                                            </td>
                                                        </tr>
                                                    ))}
                                                </tbody>
                                            </table>
                                        </div>
                                    )}
                                </section>
                            )}

                            {/* Media panel — only when at least one mode
                                has activity. Reuses the same per-mode
                                layout as GroupUsageTab's Media section
                                so the two views read consistently. */}
                            {media && media.request_count > 0 && (
                                <section>
                                    <header className="flex items-center gap-2 mb-2">
                                        <ImageIcon className="w-4 h-4 text-muted" />
                                        <h3 className="text-sm font-semibold text-default">
                                            Media ({fmt(media.request_count)} request{media.request_count === 1 ? '' : 's'})
                                        </h3>
                                        <span className="ml-auto text-xs text-muted">
                                            {fmtCost(media.estimated_cost_usd)} · already counted in LLM total
                                        </span>
                                    </header>
                                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                                        {(Object.entries(media.modes) as [MediaMode, NonNullable<typeof media.modes[MediaMode]>][])
                                            .filter(([, m]) => m.request_count > 0)
                                            .map(([mode, m]) => {
                                                const meta = MEDIA_MODE_META[mode];
                                                const Icon = meta.Icon;
                                                const unit =
                                                    mode === 'image_generation'
                                                        ? `${fmt(m.images_generated)} image${m.images_generated === 1 ? '' : 's'}`
                                                        : mode === 'audio_speech'
                                                          ? `${fmt(m.audio_chars)} chars`
                                                          : mode === 'audio_transcription'
                                                            ? `${m.audio_seconds_input.toFixed(1)}s in`
                                                            : `${m.video_seconds_output.toFixed(1)}s out`;
                                                return (
                                                    <div key={mode} className="border border-default rounded-lg p-3 bg-surface">
                                                        <div className="flex items-center gap-1.5 text-xs text-muted mb-1">
                                                            <Icon className="w-3.5 h-3.5" />
                                                            <span>{meta.label}</span>
                                                        </div>
                                                        <div className="text-base font-semibold text-default tabular-nums">
                                                            {fmt(m.request_count)} req · {unit}
                                                        </div>
                                                        <div className="text-xs text-secondary mt-0.5 tabular-nums">
                                                            {fmtCost(m.cost_usd)}
                                                        </div>
                                                    </div>
                                                );
                                            })}
                                    </div>
                                </section>
                            )}
                        </div>
                    )}
                </div>

                {/* Footer */}
                <div className="px-6 py-3 border-t border-default bg-surface-alt flex justify-end">
                    <button
                        onClick={onClose}
                        className="px-4 py-1.5 text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors text-sm"
                    >
                        Close
                    </button>
                </div>
            </div>
        </div>
    );
};

export default GroupMemberUsageModal;

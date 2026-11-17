/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Signal trend charts — the run-signal timeseries rendered under each
 * signal section on the Signals tab.
 *
 * Two honesty rules carried from the endpoint:
 *   - counts (runs, outcomes) are ZERO-FILLED across the window: an
 *     empty bucket is real data (quiet), and a chart that skips quiet
 *     buckets compresses silence out of the picture;
 *   - rates and per-run costs stay `null` in quiet buckets and render
 *     as GAPS (`connectNulls={false}`): there was no cost per processed
 *     outcome in an hour that processed nothing, and drawing a line
 *     through it would invent one. Each gap is SHADED and labelled with
 *     why (`chartGaps`) — a bare blank read as a broken chart — and an
 *     hour whose processed outcomes all cost $0 is a gap on the cost
 *     chart, not $0. Empty polls (no-work runs) are never in the cost or
 *     length lines; on the outcome bars they are grey, never green.
 *
 * Lines are straight between hourly points, each point dotted, and the
 * tooltip says how many runs the point rests on: a smoothed curve drew
 * a climb from 0% to 100% between two points that had nothing between
 * them, and one run's 100% looked like a hundred runs'.
 *
 * Declared incident windows are SHADED, with a legend entry
 * that says what the shading means: runs inside a window are kept as
 * evidence but not judged. The data inside is still drawn — a window
 * excludes a period from judgement, never from view.
 */

import {
    Area,
    AreaChart,
    Bar,
    BarChart,
    CartesianGrid,
    ReferenceArea,
    ResponsiveContainer,
    Tooltip,
    XAxis,
    YAxis,
} from 'recharts';

import { useId } from 'react';

import type { RunTimeseriesPoint } from '../../lib/aiSystemTypes';
import { chartGaps, GAP_LABELS, plottedCost } from '../../lib/runTimeseries';
import { RUN_CLASS_COLORS, RUN_CLASS_LABELS } from '../../lib/runOutcomes';
import type { IncidentWindow } from '../../lib/driftResolutionTypes';
import { incidentCauseLabel, incidentShading, windowRangeText } from '../../lib/driftResolution';

/** One neutral fill for every window: it marks a period, not a severity. */
const INCIDENT_FILL = '#94a3b8';
/** Gaps: lighter than an incident window, and hatched-free — "nothing to
 *  draw here", not a declared period. */
const GAP_FILL = '#cbd5e1';
/** What each gap label means — the legend explains only the ones drawn. */
const GAP_EXPLAINED = [
    { label: GAP_LABELS.noRuns, text: 'the system made no runs in those hours.' },
    {
        label: GAP_LABELS.noProcessed,
        text: 'runs, but none ended in a declared outcome that counts toward the baseline — only empty polls (no work), failures or deviations — so there is no work to average.',
    },
    {
        label: GAP_LABELS.noPaid,
        text: 'every processed outcome cost nothing (an approved decision applied costs $0), so there is no cost to average.',
    },
    { label: GAP_LABELS.noCalls, text: 'runs, but no model or tool calls to fail.' },
];

/** The outcome bars: one stack per run class that is decisive or no-work,
 *  success first. No-work is grey, never green. */
const OUTCOME_SERIES = (['success', 'no_work', 'deviation', 'failed'] as const).map((cls) => ({
    key: cls,
    name: RUN_CLASS_LABELS[cls],
    color: RUN_CLASS_COLORS[cls],
}));

const TOOLTIP_STYLE = {
    backgroundColor: 'var(--color-surface-alt)',
    border: '1px solid var(--color-border)',
    borderRadius: '8px',
    fontSize: '12px',
    color: 'var(--color-text)',
};

function tickLabel(bucketStart: string, bucket: 'hour' | 'day'): string {
    return bucket === 'hour' ? bucketStart.slice(11, 16) : bucketStart.slice(5, 10);
}

export type TrendKind = 'cost' | 'actions' | 'errors' | 'outcomes';

const KIND_META: Record<
    TrendKind,
    { caption: string; testId: string }
> = {
    cost: { caption: 'Cost per processed outcome over time', testId: 'trend-cost' },
    actions: { caption: 'Average actions per processed outcome over time', testId: 'trend-actions' },
    errors: { caption: 'Error rate over time — model calls and tool calls', testId: 'trend-errors' },
    outcomes: { caption: 'Runs by class over time — success, no work, deviation, failed', testId: 'trend-outcomes' },
};

/** The error chart's two series — model calls first: a provider outage fails
 *  every run while every tool call succeeds. */
const ERROR_SERIES = [
    { key: 'model_error_rate', name: 'Model calls', color: '#ef4444' },
    { key: 'tool_error_rate', name: 'Tool calls', color: '#1bb5e8' },
] as const;

interface SignalTrendProps {
    kind: TrendKind;
    data: RunTimeseriesPoint[];
    bucket: 'hour' | 'day';
    /** Declared incident windows to shade. */
    incidentWindows?: IncidentWindow[];
}

export function SignalTrendChart({ kind, data, bucket, incidentWindows = [] }: SignalTrendProps) {
    const legendId = useId();
    if (!data.some((p) => p.runs > 0)) return null; // nothing ran — the tiles already say so

    const meta = KIND_META[kind];
    const ticks = data.map((p) => ({
        ...p,
        label: tickLabel(p.bucket_start, bucket),
        cost_plotted: plottedCost(p),
    }));
    const gaps = kind === 'outcomes' ? [] : chartGaps(data, kind);
    const runsAt = new Map(data.map((p) => [p.bucket_start, p.runs]));
    const tooltipLabel = (label: unknown) => {
        const key = String(label);
        const n = runsAt.get(key) ?? 0;
        return `${tickLabel(key, bucket)} · ${n} run${n === 1 ? '' : 's'}`;
    };
    const shades = incidentShading(incidentWindows, data, bucket);
    const shaded = incidentWindows.filter((w) => shades.some((s) => s.id === w.id));
    // The x axis keys on bucket_start (unique) so a window can name its
    // buckets; the tick shows the short label.
    const xAxis = (
        <XAxis dataKey="bucket_start" tick={{ fontSize: 10 }} tickFormatter={(v: string) => tickLabel(v, bucket)} />
    );
    const areas = shades.map((s) => (
        <ReferenceArea
            key={s.id}
            x1={s.x1}
            x2={s.x2}
            fill={INCIDENT_FILL}
            fillOpacity={0.22}
            stroke={INCIDENT_FILL}
            strokeDasharray="3 3"
            ifOverflow="visible"
        />
    ));

    const gapAreas = gaps.map((g) => (
        <ReferenceArea
            key={`gap-${g.x1}`}
            x1={g.x1}
            x2={g.x2}
            fill={GAP_FILL}
            fillOpacity={0.25}
            ifOverflow="visible"
            label={{ value: g.label, position: 'insideTop', fontSize: 10, fill: 'var(--color-text-muted)' }}
        />
    ));
    const lineProps = {
        type: 'linear' as const,
        strokeWidth: 2,
        fillOpacity: 0.12,
        connectNulls: false,
        dot: { r: 2 },
        activeDot: { r: 4 },
    };

    return (
        <div
            className="bg-surface border border-default rounded-lg p-3 mt-3"
            data-testid={meta.testId}
        >
            <p className="text-xs text-muted mb-2">{meta.caption}</p>
            {(kind === 'errors' || kind === 'outcomes') && (
                <p className="flex gap-3 text-[11px] text-muted mb-1" data-testid={`${meta.testId}-legend`}>
                    {(kind === 'errors' ? ERROR_SERIES : OUTCOME_SERIES).map((s) => (
                        <span key={s.key} className="flex items-center gap-1">
                            <span aria-hidden="true" className="inline-block h-0.5 w-3" style={{ backgroundColor: s.color }} />
                            {s.name}
                        </span>
                    ))}
                </p>
            )}
            <div aria-describedby={shaded.length ? legendId : undefined}>
            <ResponsiveContainer width="100%" height={140}>
                {kind === 'outcomes' ? (
                    <BarChart data={ticks} margin={{ top: 4, right: 8, left: -16, bottom: 0 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
                        {areas}
                        {xAxis}
                        <YAxis tick={{ fontSize: 10 }} allowDecimals={false} />
                        <Tooltip contentStyle={TOOLTIP_STYLE} labelFormatter={tooltipLabel} />
                        {/* Stacked by run class. No-work (an empty poll) is
                            grey: an outage hour full of successful polls must
                            not read half green. */}
                        {OUTCOME_SERIES.map((s) => (
                            <Bar key={s.key} dataKey={s.key} stackId="o" fill={s.color} name={s.name} />
                        ))}
                    </BarChart>
                ) : (
                    <AreaChart data={ticks} margin={{ top: 4, right: 8, left: -8, bottom: 0 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
                        {gapAreas}
                        {areas}
                        {xAxis}
                        <YAxis
                            tick={{ fontSize: 10 }}
                            // A rate is 0–100%; auto-scaling drew a 400% axis
                            // over a flat zero line.
                            domain={kind === 'errors' ? [0, 1] : undefined}
                            tickFormatter={(v: number) =>
                                kind === 'cost'
                                    ? `$${v}`
                                    : kind === 'errors'
                                      ? `${Math.round(v * 100)}%`
                                      : String(v)
                            }
                            width={kind === 'cost' ? 64 : 48}
                        />
                        <Tooltip
                            contentStyle={TOOLTIP_STYLE}
                            labelFormatter={tooltipLabel}
                            formatter={(v: number | string) =>
                                kind === 'cost'
                                    ? `$${Number(v).toFixed(4)}`
                                    : kind === 'errors'
                                      ? `${(Number(v) * 100).toFixed(1)}%`
                                      : Number(v).toFixed(1)
                            }
                        />
                        {kind === 'errors' ? (
                            ERROR_SERIES.map((s) => (
                                <Area
                                    key={s.key}
                                    {...lineProps}
                                    dataKey={s.key}
                                    name={s.name}
                                    stroke={s.color}
                                    fill={s.color}
                                />
                            ))
                        ) : (
                            <Area
                                {...lineProps}
                                dataKey={kind === 'cost' ? 'cost_plotted' : 'avg_actions'}
                                name={KIND_META[kind].caption}
                                stroke="#1bb5e8"
                                fill="#1bb5e8"
                            />
                        )}
                    </AreaChart>
                )}
            </ResponsiveContainer>
            </div>
            {gaps.length > 0 && (
                <p className="mt-2 flex items-center gap-2 text-xs text-muted" data-testid={`${meta.testId}-gap-legend`}>
                    <span
                        aria-hidden="true"
                        className="inline-block h-3 w-4 rounded-sm"
                        style={{ backgroundColor: GAP_FILL, opacity: 0.6 }}
                    />
                    <span>
                        Light shading: nothing to plot.
                        {GAP_EXPLAINED.filter((g) => gaps.some((x) => x.label.includes(g.label))).map((g) => (
                            <span key={g.label}>
                                {' '}<strong className="font-medium">{g.label}</strong> — {g.text}
                            </span>
                        ))}
                    </span>
                </p>
            )}
            {shaded.length > 0 && (
                <div className="mt-2 text-xs text-muted" id={legendId} data-testid={`${meta.testId}-incident-legend`}>
                    <p className="flex items-center gap-2">
                        <span
                            aria-hidden="true"
                            className="inline-block h-3 w-4 rounded-sm border border-dashed"
                            style={{ backgroundColor: INCIDENT_FILL, opacity: 0.5, borderColor: INCIDENT_FILL }}
                        />
                        <span>
                            Shaded: declared incident window — runs inside are kept as evidence but not judged.
                        </span>
                    </p>
                    <ul className="mt-1 ml-6 list-disc">
                        {shaded.map((w) => (
                            <li key={w.id} data-testid="incident-legend-item">
                                {incidentCauseLabel(w.cause_category)} · {windowRangeText(w)}
                            </li>
                        ))}
                    </ul>
                </div>
            )}
        </div>
    );
}

export default SignalTrendChart;

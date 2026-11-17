/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Governance → AI Assets → Factsheet.
 *
 * Per-asset governance drill-down opened from a row in the AI Assets
 * table (NOT a nav item). Renders a point-in-time roll-up of an asset's
 * usage/cost, guardrail activity, compliance-framework implication,
 * residency routing and access bindings over a configurable window, plus
 * the ability to capture immutable snapshots for audit evidence and view
 * historical ones through the same section layout.
 *
 * Tenant comes from the JWT (AI Assets lives under the tenant-admin
 * Governance group) — no tenant_id is passed. Data fetching is
 * hand-rolled (useState/useEffect/useCallback) — NO react-query.
 */

import React, { useCallback, useEffect, useState } from 'react';
import { TABLE_TH_TYPE } from '../ui/tableStyles';
import {
    ArrowLeft,
    Download,
    FileBarChart,
    FileJson,
    Loader2,
    Camera,
    History,
} from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { useToast } from '../common/Toast';
import InfoBanner from '../common/InfoBanner';
import { ASSET_KIND_LABEL } from '../../lib/aiAssetTypes';
import type { AssetKind } from '../../lib/aiAssetTypes';
import {
    FACTSHEET_PERIODS,
    FACTSHEET_DEFAULT_PERIOD_HOURS,
} from '../../lib/factsheetTypes';
import type {
    Factsheet,
    FactsheetSnapshot,
} from '../../lib/factsheetTypes';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

interface AIAssetFactsheetProps {
    assetId: string;
    assetName: string;
    onBack: () => void;
}

// ---- helpers ----------------------------------------------------------

function formatDate(s?: string | null): string {
    if (!s) return '—';
    const d = new Date(s);
    return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString();
}

function formatInt(n: number): string {
    return Number.isFinite(n) ? n.toLocaleString() : '—';
}

function formatUsd(n: number): string {
    if (!Number.isFinite(n)) return '—';
    return `$${n.toLocaleString(undefined, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 4,
    })}`;
}

function formatPct(n: number): string {
    if (!Number.isFinite(n)) return '—';
    return `${(n * 100).toLocaleString(undefined, { maximumFractionDigits: 2 })}%`;
}

function periodLabel(hours: number): string {
    const match = FACTSHEET_PERIODS.find((p) => p.hours === hours);
    return match ? match.label : `${hours}h`;
}

/** Render an arbitrary config value as a compact, readable string. */
function renderValue(value: unknown): string {
    if (value === null || value === undefined) return '—';
    if (typeof value === 'boolean') return value ? 'Yes' : 'No';
    if (typeof value === 'string' || typeof value === 'number') {
        return String(value);
    }
    if (Array.isArray(value)) {
        return value.length ? value.map((v) => String(v)).join(', ') : '—';
    }
    try {
        return JSON.stringify(value);
    } catch {
        return String(value);
    }
}

function humanizeKey(key: string): string {
    return key
        .replace(/_/g, ' ')
        .replace(/\b\w/g, (c) => c.toUpperCase());
}

function triggerDownload(blob: Blob, filename: string) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

// ---- small presentational atoms ---------------------------------------

const Card: React.FC<{ title: string; children: React.ReactNode }> = ({
    title,
    children,
}) => (
    <div className="bg-surface border border-default rounded-lg shadow-sm overflow-hidden">
        <div className="px-5 py-3 border-b border-default bg-surface-alt">
            <h2 className="text-sm font-semibold text-default">{title}</h2>
        </div>
        <div className="p-5">{children}</div>
    </div>
);

const StatTile: React.FC<{ label: string; value: string; sub?: string }> = ({
    label,
    value,
    sub,
}) => (
    <div className="bg-surface-alt border border-default rounded-lg p-4">
        <div className={TABLE_TH_TYPE}>
            {label}
        </div>
        <div className="mt-1 text-xl font-semibold text-default">{value}</div>
        {sub && <div className="mt-0.5 text-xs text-muted">{sub}</div>}
    </div>
);

const Chip: React.FC<{ children: React.ReactNode }> = ({ children }) => (
    <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-surface-raised text-secondary border border-default">
        {children}
    </span>
);

const KeyValueRow: React.FC<{ label: string; value: string }> = ({
    label,
    value,
}) => (
    <div className="flex items-start justify-between gap-4 py-1.5 border-b border-default last:border-0">
        <span className="text-xs text-muted">{label}</span>
        <span className="text-sm text-default text-right break-all">{value}</span>
    </div>
);

// ---- factsheet body (shared by live + snapshot view) ------------------

const FactsheetSections: React.FC<{ fs: Factsheet }> = ({ fs }) => {
    const { asset, config, usage, guardrails, compliance, residency, access } =
        fs;

    const assetKind =
        typeof asset.asset_kind === 'string'
            ? (asset.asset_kind as AssetKind)
            : undefined;
    const tags = Array.isArray(asset.tags)
        ? (asset.tags as unknown[]).map((t) => String(t))
        : [];

    const overviewFields: { label: string; value: string }[] = [
        { label: 'Kind', value: assetKind ? ASSET_KIND_LABEL[assetKind] : '—' },
        { label: 'Ref ID', value: renderValue(asset.ref_id) },
        { label: 'Owner', value: renderValue(asset.owner) },
        { label: 'Lifecycle stage', value: renderValue(asset.lifecycle_stage) },
        { label: 'Risk tier', value: renderValue(asset.risk_tier) },
        {
            label: 'Enforcement mode',
            value: asset.enforcement_mode && asset.enforcement_mode !== 'off'
                ? `${asset.enforcement_mode} — not available in this release: it has no effect`
                : renderValue(asset.enforcement_mode),
        },
        { label: 'Status', value: renderValue(asset.status) },
    ];

    const frameworkEntries = Object.entries(compliance.frameworks);
    const residencyClassEntries = Object.entries(
        residency.residency_class_counts,
    );
    const configEntries = Object.entries(config);

    return (
        <div className="space-y-6">
            {/* Overview */}
            <Card title="Overview">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8">
                    <div>
                        {overviewFields.map((f) => (
                            <KeyValueRow
                                key={f.label}
                                label={f.label}
                                value={f.value}
                            />
                        ))}
                    </div>
                    <div>
                        {configEntries.length === 0 ? (
                            <p className="text-sm text-muted py-1.5">
                                No configuration captured for this asset.
                            </p>
                        ) : (
                            configEntries.map(([k, v]) => (
                                <KeyValueRow
                                    key={k}
                                    label={humanizeKey(k)}
                                    value={renderValue(v)}
                                />
                            ))
                        )}
                    </div>
                </div>
                {tags.length > 0 && (
                    <div className="mt-4 flex flex-wrap gap-1.5">
                        {tags.map((t) => (
                            <Chip key={t}>{t}</Chip>
                        ))}
                    </div>
                )}
            </Card>

            {/* Usage & cost */}
            <Card title="Usage & cost">
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                    <StatTile
                        label="Requests"
                        value={formatInt(usage.requests)}
                        sub={
                            usage.observed_requests > 0
                                ? `${formatInt(usage.observed_requests)} observed (imported)`
                                : undefined
                        }
                    />
                    <StatTile
                        label="Total tokens"
                        value={formatInt(usage.total_tokens)}
                        sub={`${formatInt(usage.prompt_tokens)} prompt · ${formatInt(
                            usage.completion_tokens,
                        )} completion`}
                    />
                    <StatTile
                        label="Estimated cost"
                        value={formatUsd(usage.estimated_cost_usd)}
                        sub={
                            usage.observed_cost_usd > 0
                                ? `${formatUsd(usage.observed_cost_usd)} observed (imported)`
                                : undefined
                        }
                    />
                    <StatTile
                        label="Avg duration"
                        value={`${formatInt(usage.avg_duration_ms)} ms`}
                    />
                    <StatTile
                        label="Successes"
                        value={formatInt(usage.success_count)}
                    />
                    <StatTile label="Errors" value={formatInt(usage.error_count)} />
                    <StatTile
                        label="Error rate"
                        value={formatPct(usage.error_rate)}
                    />
                    <StatTile
                        label="First / last seen"
                        value={formatDate(usage.first_seen)}
                        sub={formatDate(usage.last_seen)}
                    />
                </div>
            </Card>

            {/* Guardrails */}
            <Card title="Guardrails">
                <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                    <StatTile
                        label="Guardrail blocks"
                        value={formatInt(guardrails.guardrail_blocked)}
                    />
                    <StatTile
                        label="PII detected"
                        value={formatInt(guardrails.pii_detected)}
                    />
                    <StatTile
                        label="Argument-policy denials"
                        value={formatInt(guardrails.argument_policy_denied)}
                    />
                </div>
            </Card>

            {/* Compliance frameworks */}
            <Card title="Compliance frameworks">
                {frameworkEntries.length === 0 ? (
                    <p className="text-sm text-muted">
                        No frameworks implicated in this window.
                    </p>
                ) : (
                    <div className="flex flex-wrap gap-2">
                        {frameworkEntries.map(([framework, count]) => (
                            <span
                                key={framework}
                                className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium bg-blue-50 text-blue-700 border border-blue-200"
                            >
                                <span>{framework}</span>
                                <span className="font-semibold">
                                    {formatInt(count)}
                                </span>
                            </span>
                        ))}
                    </div>
                )}
            </Card>

            {/* Residency */}
            <Card title="Residency">
                <div className="space-y-4">
                    <div>
                        <div className={`${TABLE_TH_TYPE} mb-2`}>
                            Regions routed to
                        </div>
                        {residency.regions_routed_to.length === 0 ? (
                            <p className="text-sm text-muted">
                                No routing observed in this window.
                            </p>
                        ) : (
                            <div className="flex flex-wrap gap-1.5">
                                {residency.regions_routed_to.map((r) => (
                                    <Chip key={r}>{r}</Chip>
                                ))}
                            </div>
                        )}
                    </div>
                    <div>
                        <div className={`${TABLE_TH_TYPE} mb-2`}>
                            Residency classes
                        </div>
                        {residencyClassEntries.length === 0 ? (
                            <p className="text-sm text-muted">
                                No residency classes recorded.
                            </p>
                        ) : (
                            <div className="flex flex-wrap gap-3">
                                {residencyClassEntries.map(([cls, count]) => (
                                    <div
                                        key={cls}
                                        className="text-sm text-default"
                                    >
                                        <span className="text-muted">{cls}:</span>{' '}
                                        <span className="font-semibold">
                                            {formatInt(count)}
                                        </span>
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>
                </div>
            </Card>

            {/* Access */}
            <Card title="Access">
                {access.resource_groups.length === 0 ? (
                    <p className="text-sm text-muted">
                        Not bound to any resource group.
                    </p>
                ) : (
                    <div className="flex flex-wrap gap-1.5">
                        {access.resource_groups.map((rg) => (
                            <Chip key={rg.id}>{rg.name || rg.id}</Chip>
                        ))}
                    </div>
                )}
            </Card>
        </div>
    );
};

// ---- main component ---------------------------------------------------

const AIAssetFactsheet: React.FC<AIAssetFactsheetProps> = ({
    assetId,
    assetName,
    onBack,
}) => {
    const { gatewayClient } = useGateway();
    const toast = useToast();

    const [periodHours, setPeriodHours] = useState<number>(
        FACTSHEET_DEFAULT_PERIOD_HOURS,
    );
    const [factsheet, setFactsheet] = useState<Factsheet | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    const [capturing, setCapturing] = useState(false);
    const [exportOpen, setExportOpen] = useState(false);

    const [snapshots, setSnapshots] = useState<FactsheetSnapshot[]>([]);
    const [snapshotsLoading, setSnapshotsLoading] = useState(false);
    const [readFailure, reads] = useReadFailures<'snapshots'>();

    // When non-null, we're viewing a captured snapshot instead of live data.
    const [viewing, setViewing] = useState<FactsheetSnapshot | null>(null);

    const refresh = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const fs = await gatewayClient.getFactsheet(assetId, {
                period_hours: periodHours,
            });
            setFactsheet(fs);
        } catch (e: unknown) {
            const msg = e instanceof Error ? e.message : String(e);
            setError(msg);
        } finally {
            setLoading(false);
        }
    }, [gatewayClient, assetId, periodHours]);

    const refreshSnapshots = useCallback(async () => {
        setSnapshotsLoading(true);
        try {
            const res = await gatewayClient.listFactsheetSnapshots(assetId);
            setSnapshots(res.snapshots);
            reads.loaded('snapshots');
        } catch (err) {
            // The page stays; the history says it could not be loaded.
            setSnapshots([]);
            reads.lost('snapshots', 'Failed to load the snapshot history', err);
        } finally {
            setSnapshotsLoading(false);
        }
    }, [gatewayClient, assetId, reads]);

    useEffect(() => {
        runBackground(refresh(), 'refresh');
    }, [refresh]);

    useEffect(() => {
        runBackground(refreshSnapshots(), 'snapshots');
    }, [refreshSnapshots]);

    async function handleCapture() {
        setCapturing(true);
        try {
            await gatewayClient.captureFactsheetSnapshot(assetId, periodHours);
            toast.success('Snapshot captured');
            await refreshSnapshots();
        } catch (e: unknown) {
            const msg = e instanceof Error ? e.message : String(e);
            toast.error('Snapshot failed', msg);
        } finally {
            setCapturing(false);
        }
    }

    // The factsheet currently shown — live or the snapshot being viewed.
    const shown: Factsheet | null = viewing ? viewing.snapshot_json : factsheet;

    function handleExportJson() {
        setExportOpen(false);
        if (!shown) return;
        const blob = new Blob([JSON.stringify(shown, null, 2)], {
            type: 'application/json',
        });
        triggerDownload(blob, `factsheet-${assetId}.json`);
    }

    async function handleExportCsv() {
        setExportOpen(false);
        try {
            const blob = await gatewayClient.exportFactsheetCsv(assetId, {
                period_hours: periodHours,
            });
            triggerDownload(blob, `factsheet-${assetId}.csv`);
        } catch (e: unknown) {
            const msg = e instanceof Error ? e.message : String(e);
            toast.error('Export failed', msg);
        }
    }

    const assetKind =
        shown && typeof shown.asset.asset_kind === 'string'
            ? (shown.asset.asset_kind as AssetKind)
            : undefined;

    return (
        <div className="p-6 space-y-6">
            {/* Header */}
            <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                    <button
                        onClick={onBack}
                        className="inline-flex items-center gap-1.5 text-sm font-medium text-secondary hover:text-default transition-colors mb-2"
                    >
                        <ArrowLeft className="w-4 h-4" />
                        <span>AI Assets</span>
                    </button>
                    <div className="flex items-center gap-2">
                        <div className="p-2 rounded-lg bg-primary-100">
                            <FileBarChart className="w-5 h-5 text-primary-600" />
                        </div>
                        <h1 className="text-2xl font-bold text-default truncate">
                            {assetName}
                        </h1>
                        {assetKind && <Chip>{ASSET_KIND_LABEL[assetKind]}</Chip>}
                    </div>
                </div>

                <div className="flex items-center gap-2 flex-shrink-0">
                    <select
                        value={periodHours}
                        onChange={(e) => setPeriodHours(Number(e.target.value))}
                        className="px-3 py-2 rounded-lg border border-strong bg-surface text-sm text-default focus:outline-none focus:ring-2 focus:ring-primary-500"
                        aria-label="Factsheet period"
                    >
                        {FACTSHEET_PERIODS.map((p) => (
                            <option key={p.hours} value={p.hours}>
                                {p.label}
                            </option>
                        ))}
                    </select>

                    {/* Export */}
                    <div className="relative">
                        <button
                            onClick={() => setExportOpen((v) => !v)}
                            className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt transition-colors"
                        >
                            <Download className="w-4 h-4" />
                            <span>Export</span>
                        </button>
                        {exportOpen && (
                            <div className="absolute right-0 mt-1 w-44 bg-surface border border-default rounded-lg shadow-lg z-10 overflow-hidden">
                                <button
                                    onClick={handleExportJson}
                                    className="flex items-center gap-2 w-full px-3 py-2 text-sm text-default hover:bg-surface-alt transition-colors"
                                >
                                    <FileJson className="w-4 h-4 text-muted" />
                                    <span>Export JSON</span>
                                </button>
                                <button
                                    onClick={asyncHandler(handleExportCsv, 'click')}
                                    className="flex items-center gap-2 w-full px-3 py-2 text-sm text-default hover:bg-surface-alt transition-colors"
                                >
                                    <Download className="w-4 h-4 text-muted" />
                                    <span>Export CSV</span>
                                </button>
                            </div>
                        )}
                    </div>

                    <button
                        onClick={asyncHandler(handleCapture, 'click')}
                        disabled={capturing}
                        className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-primary-500 hover:bg-primary-600 rounded-lg disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                    >
                        {capturing ? (
                            <Loader2 className="w-4 h-4 animate-spin" />
                        ) : (
                            <Camera className="w-4 h-4" />
                        )}
                        <span>
                            {capturing ? 'Capturing…' : 'Capture snapshot'}
                        </span>
                    </button>
                </div>
            </div>

            {/* Viewing-snapshot banner */}
            {viewing && (
                <InfoBanner variant="info" className="mb-0">
                    <p>
                        Viewing snapshot from{' '}
                        <strong>{formatDate(viewing.created_at)}</strong> —{' '}
                        <button
                            onClick={() => setViewing(null)}
                            className="underline font-medium hover:no-underline"
                        >
                            Back to live
                        </button>
                    </p>
                </InfoBanner>
            )}

            {error && !viewing && (
                <InfoBanner variant="error" className="mb-0">
                    <p>{error}</p>
                </InfoBanner>
            )}

            {/* Body */}
            {loading && !viewing ? (
                <div className="flex items-center justify-center py-16">
                    <Loader2 className="w-8 h-8 text-primary-600 animate-spin" />
                </div>
            ) : shown ? (
                <>
                    <FactsheetSections fs={shown} />

                    {/* Meta caption */}
                    <p className="text-xs text-muted">
                        Generated {formatDate(shown.meta.generated_at)} ·{' '}
                        {periodLabel(shown.meta.period_hours)} · window{' '}
                        {formatDate(shown.meta.window_start)} →{' '}
                        {formatDate(shown.meta.window_end)}
                    </p>
                </>
            ) : null}

            {/* Snapshot history */}
            <Card title="Snapshot history">
                {snapshotsLoading ? (
                    <div className="flex items-center justify-center py-6">
                        <Loader2 className="w-5 h-5 text-primary-600 animate-spin" />
                    </div>
                ) : readFailure('snapshots') ? (
                    <ReadFailureNotice compact failure={readFailure('snapshots')} onRetry={() => runBackground(refreshSnapshots(), 'snapshots')} />
                ) : snapshots.length === 0 ? (
                    <div className="flex items-center gap-2 text-sm text-muted">
                        <History className="w-4 h-4" />
                        <span>No snapshots captured yet.</span>
                    </div>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="min-w-full divide-y divide-default">
                            <thead className="bg-surface-alt border-b border-default">
                                <tr>
                                    <th className={`${TABLE_TH_TYPE} px-3 py-2 text-left`}>
                                        Captured
                                    </th>
                                    <th className={`${TABLE_TH_TYPE} px-3 py-2 text-left`}>
                                        Period
                                    </th>
                                    <th className={`${TABLE_TH_TYPE} px-3 py-2 text-left`}>
                                        Trigger
                                    </th>
                                    <th className={`${TABLE_TH_TYPE} px-3 py-2 text-right`}>
                                        Actions
                                    </th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-default">
                                {snapshots.map((s) => (
                                    <tr
                                        key={s.id}
                                        className="hover:bg-surface-alt transition-colors"
                                    >
                                        <td className="px-3 py-2 text-sm text-default whitespace-nowrap">
                                            {formatDate(s.created_at)}
                                        </td>
                                        <td className="px-3 py-2 text-sm text-secondary whitespace-nowrap">
                                            {periodLabel(s.period_hours)}
                                        </td>
                                        <td className="px-3 py-2 text-sm text-secondary whitespace-nowrap">
                                            {s.trigger || '—'}
                                        </td>
                                        <td className="px-3 py-2 text-right whitespace-nowrap">
                                            <button
                                                onClick={() => setViewing(s)}
                                                className="px-3 py-1.5 text-sm font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt transition-colors"
                                            >
                                                View
                                            </button>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </Card>
        </div>
    );
};

export default AIAssetFactsheet;

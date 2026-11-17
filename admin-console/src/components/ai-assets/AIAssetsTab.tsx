/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Governance → AI Assets page.
 *
 * The Asset Register is the auto-discovered inventory of every
 * governed AI asset (LLM models/providers, routing groups, MCP
 * servers, skills, agent cards) with a governance overlay: owner,
 * lifecycle stage, risk tier, enforcement mode, tags, review status.
 *
 * "Sync now" rescans the platform config and reconciles the registry
 * (creates newly-seen assets, updates changed ones, flags orphaned
 * rows whose backing config disappeared). Operators can also register
 * a *manual* asset and curate the governance fields on any row.
 *
 * Lives under the Governance nav group (`requiresTenantAdmin`), so the
 * tenant comes from the JWT — no tenant_id is passed (mirrors
 * ComplianceTab). Data fetching is hand-rolled (useState/useEffect/
 * useCallback) — NO react-query.
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
    Box,
    Boxes,
    BookOpen,
    Eye,
    FileBarChart,
    Loader2,
    Pencil,
    Plus,
    RefreshCw,
    Search,
    Trash2,
} from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { useToast } from '../common/Toast';
import { useConfirm } from '../common/ConfirmationDialog';
import InfoBanner from '../common/InfoBanner';
import { ICON_REGISTRY } from '../../lib/lucideIconMap';
import AIAssetEditDialog from './AIAssetEditDialog';
import AIAssetFactsheet from './AIAssetFactsheet';
import AIAssetGuideDialog from './AIAssetGuideDialog';
import { formatRegisterDate as formatDate } from './registerTable';
import { TABLE_TH_TYPE } from '../ui/tableStyles';
import { useSlot } from '../../app/slots';
import type { AssetCoverageData } from '../../lib/assetCoverageTypes';
import type {
    AIAsset,
    AssetKind,
    AssetStatus,
    LifecycleStage,
    RiskTier,
} from '../../lib/aiAssetTypes';
import {
    ASSET_KIND_LABEL,
    ENFORCEMENT_MODE_LABEL,
    LIFECYCLE_STAGE_LABEL,
    RISK_TIER_LABEL,
    STATUS_LABEL,
} from '../../lib/aiAssetTypes';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

const PAGE_LIMIT = 100;

const TH = `px-6 py-3 text-left ${TABLE_TH_TYPE}`;

const STATUS_BADGE: Record<AssetStatus, string> = {
    ok: 'bg-green-50 text-green-700 border-green-200',
    review_due: 'bg-amber-50 text-amber-700 border-amber-200',
    flagged: 'bg-red-50 text-red-700 border-red-200',
};

// Stable initials-fallback palette (mirrors ProviderIcon.tsx) so the
// neutral/error fallback for an asset always looks the same per row.
const FALLBACK_PALETTE = [
    '#6366f1', // indigo
    '#0ea5e9', // sky
    '#10b981', // emerald
    '#f59e0b', // amber
    '#ef4444', // red
    '#8b5cf6', // violet
    '#ec4899', // pink
    '#14b8a6', // teal
];

function paletteIndex(key: string): number {
    let hash = 0;
    for (let i = 0; i < key.length; i++) {
        hash = (hash * 31 + key.charCodeAt(i)) | 0;
    }
    return Math.abs(hash) % FALLBACK_PALETTE.length;
}

const ICON_PX = 24;

/**
 * Per-asset icon. Handles the three `icon_url` shapes shipped by the
 * registry backend:
 *   (a) a normal URL → <img> with an initials-letter fallback on error;
 *   (b) a `lucide:{name}` sentinel (skills) → the mapped Lucide component;
 *   (c) null/empty → a neutral kind-based letter fallback (generic box icon
 *       when there's nothing to letter from).
 */
const AssetIcon: React.FC<{ asset: AIAsset }> = ({ asset }) => {
    const { gatewayClient } = useGateway();
    const [imgError, setImgError] = useState(false);
    const dimension = `${ICON_PX}px`;
    const radius = `${Math.round(ICON_PX * 0.2)}px`;
    const baseStyle: React.CSSProperties = {
        width: dimension,
        height: dimension,
        borderRadius: radius,
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        flexShrink: 0,
        overflow: 'hidden',
    };

    const label = asset.display_name || asset.ref_id || asset.asset_kind;
    const letterFallback = (
        <span
            aria-label={label}
            title={label}
            style={{
                ...baseStyle,
                background: FALLBACK_PALETTE[paletteIndex(asset.ref_id || asset.id)],
                color: 'var(--color-on-accent)',
                fontSize: `${Math.round(ICON_PX * 0.42)}px`,
                fontWeight: 600,
                letterSpacing: '-0.02em',
            }}
        >
            {label.charAt(0).toUpperCase()}
        </span>
    );

    const iconUrl = asset.icon_url?.trim();

    // (b) Lucide sentinel — `lucide:{name}` (skills).
    if (iconUrl?.startsWith('lucide:')) {
        const name = iconUrl.slice('lucide:'.length);
        const entry = ICON_REGISTRY[name];
        if (entry) {
            const Cmp = entry.component;
            return (
                <span style={{ ...baseStyle, background: 'var(--color-surface-raised)', color: 'var(--color-text-secondary)' }}>
                    <Cmp width={Math.round(ICON_PX * 0.62)} height={Math.round(ICON_PX * 0.62)} />
                </span>
            );
        }
        // Unknown lucide name → neutral generic icon.
        return (
            <span style={{ ...baseStyle, background: 'var(--color-surface-raised)', color: 'var(--color-text-secondary)' }}>
                <Box width={Math.round(ICON_PX * 0.62)} height={Math.round(ICON_PX * 0.62)} />
            </span>
        );
    }

    // (a) Normal URL → <img>, with initials fallback on load error.
    if (iconUrl && !imgError) {
        return (
            <img
                src={gatewayClient.resolveAssetUrl(iconUrl)}
                alt={label}
                width={ICON_PX}
                height={ICON_PX}
                style={{ ...baseStyle, objectFit: 'contain', background: 'var(--color-surface)' }}
                loading="lazy"
                onError={() => setImgError(true)}
            />
        );
    }

    // (c) null/empty (or a broken <img>) → neutral letter fallback.
    return letterFallback;
};

const AIAssetsTab: React.FC = () => {
    const { gatewayClient } = useGateway();
    const toast = useToast();
    const confirm = useConfirm();

    const [assets, setAssets] = useState<AIAsset[]>([]);
    const [total, setTotal] = useState(0);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [syncing, setSyncing] = useState(false);

    // Coverage panels come from the `asset-register.coverage` slot. What they
    // show is tenant-wide and filter-independent (unlike the register), so it
    // is loaded separately and treated as best-effort: a failure hides the
    // panels, never the table. Keyed by the contributing item's id.
    const coverageItems = useSlot('asset-register.coverage');
    const [coverage, setCoverage] = useState<Readonly<Record<string, AssetCoverageData | null>>>({});

    // Filters
    const [assetKind, setAssetKind] = useState<AssetKind | ''>('');
    const [lifecycleStage, setLifecycleStage] = useState<LifecycleStage | ''>('');
    const [riskTier, setRiskTier] = useState<RiskTier | ''>('');
    const [status, setStatus] = useState<AssetStatus | ''>('');
    const [q, setQ] = useState('');
    const [offset, setOffset] = useState(0);

    // Dialog
    const [creating, setCreating] = useState(false);
    const [editing, setEditing] = useState<AIAsset | null>(null);
    const [guideOpen, setGuideOpen] = useState(false);
    // Full-page views opened from the header come from the
    // `asset-register.views` slot. They feed or explain this register, so they
    // live behind a button here rather than a nav item.
    const views = useSlot('asset-register.views');
    const [openView, setOpenView] = useState<string | null>(null);

    // Factsheet drill-down (per-asset; replaces the table when set).
    const [factsheetAsset, setFactsheetAsset] = useState<{
        id: string;
        name: string;
    } | null>(null);

    const refresh = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const res = await gatewayClient.listAIAssets({
                asset_kind: assetKind || undefined,
                lifecycle_stage: lifecycleStage || undefined,
                risk_tier: riskTier || undefined,
                status: status || undefined,
                q: q.trim() || undefined,
                limit: PAGE_LIMIT,
                offset,
            });
            setAssets(res.assets);
            setTotal(res.total);
        } catch (e: unknown) {
            const msg = e instanceof Error ? e.message : String(e);
            setError(msg);
        } finally {
            setLoading(false);
        }
    }, [gatewayClient, assetKind, lifecycleStage, riskTier, status, q, offset]);

    useEffect(() => {
        runBackground(refresh(), 'refresh');
    }, [refresh]);

    // An item whose load fails says so where its summary would be.
    const [readFailure, reads] = useReadFailures();
    const loadCoverage = useCallback(async () => {
        if (coverageItems.length === 0) return;
        const loaded = await Promise.all(
            coverageItems.map(async (item) => {
                try {
                    const data = await item.load(gatewayClient);
                    reads.loaded(item.id);
                    return [item.id, data] as const;
                } catch (err) {
                    reads.lost(item.id, 'Failed to load a coverage panel of the register', err);
                    return [item.id, null] as const;
                }
            }),
        );
        setCoverage(Object.fromEntries(loaded));
    }, [gatewayClient, coverageItems, reads]);

    useEffect(() => {
        runBackground(loadCoverage(), 'coverage');
    }, [loadCoverage]);

    // Asset edits/creates can change the governed count and orphan flags, so
    // re-pull both the table and the coverage strip after a dialog saves.
    const onSaved = useCallback(() => {
        runBackground(refresh(), 'refresh');
        runBackground(loadCoverage(), 'coverage');
    }, [refresh, loadCoverage]);

    async function handleSync() {
        setSyncing(true);
        try {
            const { created, updated, orphaned } = await gatewayClient.syncAIAssets();
            toast.success(
                'Registry synced',
                `+${created} discovered · ${updated} updated · ${orphaned} flagged`,
            );
            setOffset(0);
            await Promise.all([refresh(), loadCoverage()]);
        } catch (e: unknown) {
            const msg = e instanceof Error ? e.message : String(e);
            toast.error('Sync failed', msg);
        } finally {
            setSyncing(false);
        }
    }

    async function handleDelete(asset: AIAsset) {
        const label = asset.display_name || asset.ref_id;
        const ok = await confirm({
            title: 'Delete asset',
            message: `Delete "${label}" from the Asset Register? Discovered assets reappear on the next sync.`,
            confirmLabel: 'Delete',
            variant: 'danger',
        });
        if (!ok) return;
        try {
            await gatewayClient.deleteAIAsset(asset.id);
            toast.success('Asset deleted');
            await Promise.all([refresh(), loadCoverage()]);
        } catch (e: unknown) {
            const msg = e instanceof Error ? e.message : String(e);
            toast.error('Delete failed', msg);
        }
    }

    function resetFilters() {
        setAssetKind('');
        setLifecycleStage('');
        setRiskTier('');
        setStatus('');
        setQ('');
        setOffset(0);
    }

    // Any filter change resets pagination to the first page.
    function onFilterChange<T>(setter: (v: T) => void) {
        return (v: T) => {
            setter(v);
            setOffset(0);
        };
    }

    const filterSelect =
        'px-3 py-2 rounded-lg border border-strong bg-surface text-sm text-default focus:outline-none focus:ring-2 focus:ring-primary-500';

    const page = Math.floor(offset / PAGE_LIMIT) + 1;
    const pageCount = Math.max(1, Math.ceil(total / PAGE_LIMIT));

    // Per-asset factsheet drill-down — replaces the table+filters entirely
    // (no new route/menu item; back button restores this view).
    if (factsheetAsset) {
        return (
            <AIAssetFactsheet
                assetId={factsheetAsset.id}
                assetName={factsheetAsset.name}
                onBack={() => setFactsheetAsset(null)}
            />
        );
    }

    // A contributed view, same state-swap pattern.
    const view = views.find((item) => item.id === openView);
    if (view) {
        return <view.View onBack={() => setOpenView(null)} />;
    }

    return (
        <div className="p-6 space-y-6">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div className="flex items-center space-x-3">
                    <div className="p-2.5 rounded-xl bg-primary-100">
                        <Boxes className="w-7 h-7 text-primary-600" />
                    </div>
                    <div>
                        <h1 className="text-2xl font-bold text-default">
                            Asset Register
                        </h1>
                        <p className="text-sm text-muted">
                            Auto-discovered inventory of governed AI assets with a
                            governance overlay.
                        </p>
                    </div>
                </div>

                <div className="flex items-center gap-2">
                    {/* Views contributed to the `asset-register.views` slot
                        (Devices and Discovery Integrations are). */}
                    {views.map((item) => (
                        <item.Button key={item.id} onOpen={() => setOpenView(item.id)} />
                    ))}

                    <button
                        onClick={() => setGuideOpen(true)}
                        className="flex items-center space-x-2 px-4 py-2 text-sm font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt transition-colors"
                    >
                        <BookOpen className="w-4 h-4" />
                        <span>Guide for operators &amp; auditors</span>
                    </button>

                    <button
                        onClick={asyncHandler(handleSync, 'click')}
                        disabled={syncing}
                        className="flex items-center space-x-2 px-4 py-2 text-sm font-medium text-white bg-primary-500 hover:bg-primary-600 rounded-lg disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                    >
                        {syncing ? (
                            <Loader2 className="w-4 h-4 animate-spin" />
                        ) : (
                            <RefreshCw className="w-4 h-4" />
                        )}
                        <span>{syncing ? 'Syncing…' : 'Sync now'}</span>
                    </button>
                </div>
            </div>

            <InfoBanner title="What the Asset Register tracks">
                <p>
                    The registry is the auto-discovered inventory of every governed AI
                    asset on this tenant — LLM models and providers, routing groups,
                    MCP servers, skills, and agent cards. <strong>Sync now</strong>{' '}
                    rescans the platform configuration and reconciles the registry:
                    newly-seen assets are created, changed ones updated, and rows whose
                    backing configuration has disappeared are flagged as orphaned. Layer
                    governance on each asset — owner, lifecycle stage, risk tier,
                    intended use and tags — or register a manual
                    asset that the discovery scan doesn&apos;t see. The coverage
                    scorecard below splits assets across three planes:{' '}
                    <strong>Governed</strong> (configured and enforced inline),{' '}
                    <strong>Observed</strong> (imported from vendor traffic for
                    cost/usage visibility but not enforced — enable one to govern it
                    inline), and <strong>Ungoverned</strong> &ldquo;shadow AI&rdquo;
                    the out-of-band discovery plane has seen but that isn&apos;t
                    configured in the gateway.
                </p>
            </InfoBanner>

            {/* Coverage panels contributed to the `asset-register.coverage`
                slot: the summary here, the detail below the register. */}
            {coverageItems.map((item) => {
                const data = coverage[item.id];
                return data ? (
                    <item.Summary key={item.id} data={data} />
                ) : (
                    <ReadFailureNotice key={item.id} failure={readFailure(item.id)} onRetry={() => runBackground(loadCoverage(), 'coverage')} />
                );
            })}

            {/* Filters */}
            <div className="flex flex-wrap items-center gap-3">
                <select
                    value={assetKind}
                    onChange={(e) =>
                        onFilterChange(setAssetKind)(e.target.value as AssetKind | '')
                    }
                    className={filterSelect}
                    aria-label="Filter by asset kind"
                >
                    <option value="">All kinds</option>
                    {(Object.keys(ASSET_KIND_LABEL) as AssetKind[]).map((k) => (
                        <option key={k} value={k}>
                            {ASSET_KIND_LABEL[k]}
                        </option>
                    ))}
                </select>

                <select
                    value={lifecycleStage}
                    onChange={(e) =>
                        onFilterChange(setLifecycleStage)(
                            e.target.value as LifecycleStage | '',
                        )
                    }
                    className={filterSelect}
                    aria-label="Filter by lifecycle stage"
                >
                    <option value="">All stages</option>
                    {(Object.keys(LIFECYCLE_STAGE_LABEL) as LifecycleStage[]).map((s) => (
                        <option key={s} value={s}>
                            {LIFECYCLE_STAGE_LABEL[s]}
                        </option>
                    ))}
                </select>

                <select
                    value={riskTier}
                    onChange={(e) =>
                        onFilterChange(setRiskTier)(e.target.value as RiskTier | '')
                    }
                    className={filterSelect}
                    aria-label="Filter by risk tier"
                >
                    <option value="">All risk tiers</option>
                    {(Object.keys(RISK_TIER_LABEL) as RiskTier[]).map((r) => (
                        <option key={r} value={r}>
                            {RISK_TIER_LABEL[r]}
                        </option>
                    ))}
                </select>

                <select
                    value={status}
                    onChange={(e) =>
                        onFilterChange(setStatus)(e.target.value as AssetStatus | '')
                    }
                    className={filterSelect}
                    aria-label="Filter by status"
                >
                    <option value="">All statuses</option>
                    {(Object.keys(STATUS_LABEL) as AssetStatus[]).map((s) => (
                        <option key={s} value={s}>
                            {STATUS_LABEL[s]}
                        </option>
                    ))}
                </select>

                <div className="relative flex-1 min-w-[200px] min-h-0 min-w-0">
                    <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted pointer-events-none" />
                    <input
                        type="text"
                        value={q}
                        onChange={(e) => setQ(e.target.value)}
                        onBlur={() => setOffset(0)}
                        onKeyDown={(e) => {
                            if (e.key === 'Enter') setOffset(0);
                        }}
                        placeholder="Search name, ref, owner…"
                        className="w-full pl-9 pr-3 py-2 rounded-lg border border-strong bg-surface text-sm text-default focus:outline-none focus:ring-2 focus:ring-primary-500"
                        aria-label="Search assets"
                    />
                </div>

                <button
                    onClick={resetFilters}
                    className="px-4 py-2 text-sm font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt transition-colors"
                >
                    Reset
                </button>

                <button
                    onClick={() => setCreating(true)}
                    className="flex items-center space-x-2 px-4 py-2 text-sm font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt transition-colors"
                >
                    <Plus className="w-4 h-4" />
                    <span>Add asset</span>
                </button>
            </div>

            {error && (
                <InfoBanner variant="error" className="mb-0">
                    <p>{error}</p>
                </InfoBanner>
            )}

            {/* Body */}
            {loading ? (
                <div className="flex items-center justify-center py-16">
                    <Loader2 className="w-8 h-8 text-primary-600 animate-spin" />
                </div>
            ) : assets.length === 0 ? (
                <div className="text-center py-16 bg-surface rounded-xl border-2 border-dashed border-default">
                    <Boxes className="w-10 h-10 text-muted mx-auto mb-3" />
                    <h3 className="text-base font-semibold text-default mb-1">
                        No AI assets yet
                    </h3>
                    <p className="text-sm text-muted max-w-md mx-auto">
                        Click <strong>Sync now</strong> to discover the governed AI
                        assets on this tenant, or add one manually.
                    </p>
                </div>
            ) : (
                <div className="bg-surface border border-default rounded-lg shadow-sm overflow-hidden">
                    <div className="overflow-x-auto">
                        <table className="min-w-full divide-y divide-default">
                            <thead className="bg-surface-alt">
                                <tr>
                                    <th className={TH}>Asset</th>
                                    <th className={TH}>Kind</th>
                                    <th className={TH}>Lifecycle</th>
                                    <th className={TH}>Risk</th>
                                    <th className={TH}>Status</th>
                                    <th className={TH}>Owner</th>
                                    <th className={TH}>Source</th>
                                    <th className={TH}>Enforcement</th>
                                    <th className={TH}>Updated</th>
                                    <th className={`${TH} text-right`}>Actions</th>
                                </tr>
                            </thead>
                            <tbody className="bg-surface divide-y divide-default">
                                {assets.map((a) => (
                                    <tr
                                        key={a.id}
                                        className="hover:bg-surface-alt transition-colors"
                                    >
                                        {/* Asset */}
                                        <td className="px-6 py-4">
                                            <div className="flex items-center gap-3 min-w-0">
                                                <AssetIcon asset={a} />
                                                <div className="min-w-0">
                                                    <div className="flex items-center gap-2 min-w-0">
                                                        <span className="font-medium text-default truncate max-w-xs">
                                                            {a.display_name || a.ref_id}
                                                        </span>
                                                        {a.observed && (
                                                            <span
                                                                className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-sky-50 text-sky-700 border border-sky-200 flex-shrink-0"
                                                                title="Imported from vendor traffic for cost/usage visibility — not governed inline. Enable the model to govern it."
                                                            >
                                                                <Eye className="w-3 h-3" />
                                                                Observed
                                                            </span>
                                                        )}
                                                    </div>
                                                    <div className="text-xs text-muted font-mono truncate max-w-xs">
                                                        {a.ref_id}
                                                    </div>
                                                </div>
                                            </div>
                                        </td>
                                        {/* Kind */}
                                        <td className="px-6 py-4 whitespace-nowrap">
                                            <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-surface-raised text-secondary border border-default">
                                                {ASSET_KIND_LABEL[a.asset_kind]}
                                            </span>
                                            {a.gpai_classification && (
                                                <span
                                                    data-testid={`gpai-chip-${a.id}`}
                                                    title={a.gpai_classification === 'gpai_systemic_risk'
                                                        ? 'GPAI model designated systemic-risk (Art 55 track)'
                                                        : 'General-purpose AI model (Art 53 track)'}
                                                    className="ml-1 inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase bg-purple-100 text-purple-800"
                                                >
                                                    {a.gpai_classification === 'gpai_systemic_risk' ? 'GPAI · SR' : 'GPAI'}
                                                </span>
                                            )}
                                            {a.operated_by === 'third_party' && (
                                                <span
                                                    data-testid={`thirdparty-chip-${a.id}`}
                                                    title="Operated by a third party — a supply-chain relationship, not something you run"
                                                    className="ml-1 inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase bg-amber-100 text-amber-800"
                                                >
                                                    3rd party
                                                </span>
                                            )}
                                        </td>
                                        {/* Lifecycle */}
                                        <td className="px-6 py-4 whitespace-nowrap">
                                            <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-surface-raised text-secondary border border-default">
                                                {LIFECYCLE_STAGE_LABEL[a.lifecycle_stage]}
                                            </span>
                                        </td>
                                        {/* Risk — systems own theirs; components derive it
                                            from the systems that bind them (Annex III). */}
                                        <td className="px-6 py-4 whitespace-nowrap text-xs text-secondary">
                                            {a.asset_kind === 'ai_system' ? (
                                                a.risk_tier ? RISK_TIER_LABEL[a.risk_tier] : '—'
                                            ) : a.derived_risk_tier ? (
                                                <span
                                                    data-testid={`derived-risk-${a.id}`}
                                                    title={`Derived from: ${(a.owning_systems ?? [])
                                                        .map((o) => o.name)
                                                        .join(', ')}`}
                                                    className="inline-flex items-center gap-1"
                                                >
                                                    {RISK_TIER_LABEL[a.derived_risk_tier]}
                                                    <span className="text-muted">
                                                        · via {(a.owning_systems ?? []).length} system{(a.owning_systems ?? []).length === 1 ? '' : 's'}
                                                    </span>
                                                </span>
                                            ) : (
                                                '—'
                                            )}
                                        </td>
                                        {/* Status */}
                                        <td className="px-6 py-4 whitespace-nowrap">
                                            <span
                                                className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium border ${STATUS_BADGE[a.status]}`}
                                            >
                                                {STATUS_LABEL[a.status]}
                                            </span>
                                        </td>
                                        {/* Owner */}
                                        <td className="px-6 py-4 whitespace-nowrap text-xs text-secondary">
                                            {a.owner || '—'}
                                        </td>
                                        {/* Source — where the asset came from:
                                            Imported (vendor traffic, observability
                                            only) · Manual (operator-added) ·
                                            Configured (reconciled from platform
                                            config). */}
                                        <td className="px-6 py-4 whitespace-nowrap">
                                            <span
                                                className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium border ${
                                                    a.observed
                                                        ? 'bg-sky-50 text-sky-700 border-sky-200'
                                                        : a.source === 'manual'
                                                          ? 'bg-blue-50 text-blue-700 border-blue-200'
                                                          : 'bg-surface-raised text-muted border-default'
                                                }`}
                                            >
                                                {a.observed
                                                    ? 'Imported'
                                                    : a.source === 'manual'
                                                      ? 'Manual'
                                                      : 'Configured'}
                                            </span>
                                        </td>
                                        {/* Enforcement */}
                                        <td className="px-6 py-4 whitespace-nowrap text-xs text-secondary">
                                            {ENFORCEMENT_MODE_LABEL[a.enforcement_mode]}{a.enforcement_mode !== 'off' ? ' (not available in this release)' : ''}
                                        </td>
                                        {/* Updated */}
                                        <td className="px-6 py-4 whitespace-nowrap text-xs text-muted">
                                            {formatDate(a.updated_at)}
                                        </td>
                                        {/* Actions */}
                                        <td className="px-6 py-4 whitespace-nowrap text-right">
                                            <div className="inline-flex items-center gap-1">
                                                <button
                                                    onClick={() =>
                                                        setFactsheetAsset({
                                                            id: a.id,
                                                            name:
                                                                a.display_name ||
                                                                a.ref_id,
                                                        })
                                                    }
                                                    className="p-2 text-muted hover:text-primary-600 hover:bg-surface-raised rounded-lg transition-colors"
                                                    title="View factsheet"
                                                    aria-label={`View factsheet for ${a.display_name || a.ref_id}`}
                                                >
                                                    <FileBarChart className="w-4 h-4" />
                                                </button>
                                                <button
                                                    onClick={() => setEditing(a)}
                                                    className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors"
                                                    title="Edit asset"
                                                    aria-label={`Edit ${a.display_name || a.ref_id}`}
                                                >
                                                    <Pencil className="w-4 h-4" />
                                                </button>
                                                <button
                                                    onClick={asyncHandler(() => handleDelete(a), 'click')}
                                                    className="p-2 text-muted hover:text-red-600 hover:bg-red-50 rounded-lg transition-colors"
                                                    title="Delete asset"
                                                    aria-label={`Delete ${a.display_name || a.ref_id}`}
                                                >
                                                    <Trash2 className="w-4 h-4" />
                                                </button>
                                            </div>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>

                    {/* Pager */}
                    <div className="flex items-center justify-between border-t border-default px-5 py-3">
                        <span className="text-sm text-muted">
                            {total} asset{total === 1 ? '' : 's'}
                        </span>
                        {total > PAGE_LIMIT && (
                            <div className="flex items-center gap-2">
                                <button
                                    onClick={() =>
                                        setOffset(Math.max(0, offset - PAGE_LIMIT))
                                    }
                                    disabled={offset === 0}
                                    className="px-3 py-1.5 text-sm font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                                >
                                    Prev
                                </button>
                                <span className="text-sm text-muted">
                                    Page {page} of {pageCount}
                                </span>
                                <button
                                    onClick={() => setOffset(offset + PAGE_LIMIT)}
                                    disabled={offset + PAGE_LIMIT >= total}
                                    className="px-3 py-1.5 text-sm font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                                >
                                    Next
                                </button>
                            </div>
                        )}
                    </div>
                </div>
            )}

            {coverageItems.map((item) => {
                const data = coverage[item.id];
                return data ? <item.Detail key={item.id} data={data} /> : null;
            })}

            {creating && (
                <AIAssetEditDialog
                    mode="create"
                    initial={null}
                    onClose={() => setCreating(false)}
                    onSaved={onSaved}
                />
            )}

            {editing && (
                <AIAssetEditDialog
                    mode="edit"
                    initial={editing}
                    onClose={() => setEditing(null)}
                    onSaved={onSaved}
                />
            )}

            {guideOpen && <AIAssetGuideDialog onClose={() => setGuideOpen(false)} />}
        </div>
    );
};

export default AIAssetsTab;

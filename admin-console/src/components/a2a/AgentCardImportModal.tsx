/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Import dialog for A2A Agent Cards — two modes:
 *
 *   1. Catalog (default): pulls the public a2aregistry.org snapshot
 *      via the admin API (which proxies + caches it server-side, since
 *      the registry has no CORS for browser clients), shows each agent
 *      with its icon, description, skills, and health, and lets the
 *      operator multi-select rows to bulk-import.
 *
 *   2. URL: paste a base URL or a `.well-known/agent-card.json` (or
 *      legacy `.well-known/agent.json`) — the backend tries both
 *      well-known suffixes per A2A SDK convention.
 *
 * Bulk results are returned per-row: a 409 on one card never fails the
 * batch, the modal renders a per-row pass/fail summary on completion.
 */

import React, { useEffect, useMemo, useState } from 'react';
import {
    Check,
    Download,
    Globe,
    Heart,
    Loader2,
    RefreshCw,
    Search,
    Sparkles,
    X,
    XCircle,
} from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import InfoBanner from '../common/InfoBanner';
import { useToast } from '../common/Toast';
import type {
    AgentCardBulkImportItem,
    BulkImportResult,
    CatalogAgent,
    CatalogResponse,
} from '../../lib/a2aTypes';
import { errorMessage } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';

type Mode = 'catalog' | 'url';

interface Props {
    onClose: () => void;
    /** Called after at least one card was imported successfully so the
     *  parent list can reload. The caller decides whether to also
     *  surface a toast — we only emit per-row results here. */
    onImported: (results: BulkImportResult[]) => void;
}

const AgentCardImportModal: React.FC<Props> = ({ onClose, onImported }) => {
    const { gatewayClient } = useGateway();
    const toast = useToast();

    const [mode, setMode] = useState<Mode>('catalog');

    // ── Catalog state ──────────────────────────────────────────────
    const [catalog, setCatalog] = useState<CatalogResponse | null>(null);
    const [catalogLoading, setCatalogLoading] = useState(false);
    const [catalogError, setCatalogError] = useState<string | null>(null);
    const [search, setSearch] = useState('');
    const [healthyOnly, setHealthyOnly] = useState(true);
    const [picked, setPicked] = useState<Set<string>>(new Set());

    // ── URL state ──────────────────────────────────────────────────
    const [url, setUrl] = useState('');
    const [nameOverride, setNameOverride] = useState('');

    // ── Submission state (shared) ─────────────────────────────────
    const [submitting, setSubmitting] = useState(false);
    // Whether an imported card is enabled at once; off imports it switched
    // off, to be enabled once it has been reviewed.
    const [enableImported, setEnableImported] = useState(true);
    const [results, setResults] = useState<BulkImportResult[] | null>(null);

    // Initial catalog fetch — only when the modal is in catalog mode.
    useEffect(() => {
        if (mode !== 'catalog' || catalog || catalogLoading) return;
        runBackground(loadCatalog(false), 'catalog');
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [mode]);

    async function loadCatalog(forceRefresh: boolean) {
        setCatalogLoading(true);
        setCatalogError(null);
        try {
            const resp = await gatewayClient.fetchA2ARegistryCatalog({
                force_refresh: forceRefresh || undefined,
            });
            setCatalog(resp);
        } catch (e) {
            setCatalogError(errorMessage(e) ?? 'Failed to fetch registry catalog');
        } finally {
            setCatalogLoading(false);
        }
    }

    // Apply search + health filter to the catalog rows.
    const filtered = useMemo<CatalogAgent[]>(() => {
        if (!catalog) return [];
        const q = search.trim().toLowerCase();
        return catalog.agents.filter(a => {
            if (healthyOnly && a.healthy === false) return false;
            if (!q) return true;
            const hay = [
                a.name ?? '',
                a.description ?? '',
                a.provider_name ?? '',
                a.base_url ?? '',
                ...(a.skills?.map(s => s.name ?? '') ?? []),
            ].join(' ').toLowerCase();
            return hay.includes(q);
        });
    }, [catalog, search, healthyOnly]);

    function toggle(key: string) {
        const next = new Set(picked);
        if (next.has(key)) next.delete(key);
        else next.add(key);
        setPicked(next);
    }

    function pickAllVisible() {
        const next = new Set(picked);
        for (const a of filtered) {
            const k = catalogKey(a);
            if (k) next.add(k);
        }
        setPicked(next);
    }

    function clearPicks() {
        setPicked(new Set());
    }

    async function submitCatalog() {
        if (!catalog || picked.size === 0) return;
        const items: AgentCardBulkImportItem[] = [];
        for (const a of catalog.agents) {
            const k = catalogKey(a);
            if (!k || !picked.has(k)) continue;
            // Prefer the explicit well-known URL when the registry
            // exposes it; else fall back to the base URL and let the
            // backend's well-known fallback do the rest.
            const sourceUrl = a.well_known_url || a.base_url || '';
            if (!sourceUrl) continue;
            items.push({
                source_url: sourceUrl,
                name_override: a.name ?? null,
                enabled: enableImported,
            });
        }
        if (items.length === 0) {
            toast.error('Nothing to import', 'Selected rows have no source URL.');
            return;
        }
        setSubmitting(true);
        try {
            const resp = await gatewayClient.bulkImportA2AAgentCards({
                imports: items,
            });
            setResults(resp.results);
            if (resp.succeeded > 0) onImported(resp.results);
            if (resp.failed === 0) {
                toast.success(
                    'Import complete',
                    `Imported ${resp.succeeded} card${resp.succeeded === 1 ? '' : 's'}.`,
                );
            } else {
                toast.warning(
                    'Imported with errors',
                    `${resp.succeeded} succeeded, ${resp.failed} failed.`,
                );
            }
        } catch (e) {
            toast.error('Bulk import failed', errorMessage(e) ?? 'Network error');
        } finally {
            setSubmitting(false);
        }
    }

    async function submitUrl() {
        if (!url.trim()) return;
        setSubmitting(true);
        try {
            const detail = await gatewayClient.importA2AAgentCard({
                source_url: url.trim(),
                name_override: nameOverride.trim() || null,
                enabled: enableImported,
            });
            const r: BulkImportResult = {
                source_url: url.trim(),
                success: true,
                card_id: detail.id,
                name: detail.name,
            };
            setResults([r]);
            onImported([r]);
            toast.success('Card imported', detail.name);
        } catch (e) {
            const r: BulkImportResult = {
                source_url: url.trim(),
                success: false,
                error: errorMessage(e) ?? 'Import failed',
            };
            setResults([r]);
            toast.error('Import failed', r.error ?? 'Unknown error');
        } finally {
            setSubmitting(false);
        }
    }

    return (
        <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
            onClick={onClose}
        >
            <div
                className="w-full max-w-3xl bg-surface border border-default rounded-xl shadow-xl flex flex-col"
                style={{ maxHeight: '90vh' }}
                onClick={e => e.stopPropagation()}
            >
                {/* Header */}
                <div className="border-b border-default px-5 py-4 flex items-center justify-between">
                    <div className="flex items-center space-x-2">
                        <Download className="w-5 h-5 text-primary-600" />
                        <h3 className="text-base font-semibold text-default">
                            Import Agent Cards
                        </h3>
                    </div>
                    <button
                        onClick={onClose}
                        className="p-1 text-muted hover:text-default rounded transition-colors"
                        aria-label="Close"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                {/* Mode tabs */}
                <div className="border-b border-default px-5 pt-3">
                    <div className="flex space-x-1">
                        <ModeTab
                            active={mode === 'catalog'}
                            onClick={() => { setMode('catalog'); setResults(null); }}
                            icon={<Sparkles className="w-4 h-4" />}
                            label="From Catalog"
                            sub="a2aregistry.org"
                        />
                        <ModeTab
                            active={mode === 'url'}
                            onClick={() => { setMode('url'); setResults(null); }}
                            icon={<Globe className="w-4 h-4" />}
                            label="From URL"
                            sub=".well-known/agent-card.json"
                        />
                    </div>
                </div>

                {/* Body */}
                <div className="scroll-region px-5 py-4">
                    {results ? (
                        <ResultList
                            results={results}
                            onAgain={() => { setResults(null); setPicked(new Set()); }}
                        />
                    ) : mode === 'catalog' ? (
                        <CatalogPanel
                            catalog={catalog}
                            loading={catalogLoading}
                            error={catalogError}
                            search={search}
                            onSearch={setSearch}
                            healthyOnly={healthyOnly}
                            onHealthyOnly={setHealthyOnly}
                            filtered={filtered}
                            picked={picked}
                            onToggle={toggle}
                            onPickAll={pickAllVisible}
                            onClear={clearPicks}
                            onRefresh={asyncHandler(() => loadCatalog(true), 'refresh')}
                        />
                    ) : (
                        <UrlPanel
                            url={url}
                            onUrl={setUrl}
                            nameOverride={nameOverride}
                            onNameOverride={setNameOverride}
                        />
                    )}
                </div>

                {/* Footer */}
                <div className="border-t border-default px-5 py-3 flex items-center justify-between">
                    <div className="text-xs text-muted">
                        {results
                            ? `${results.filter(r => r.success).length}/${results.length} imported`
                            : mode === 'catalog'
                                ? `${picked.size} selected${catalog ? ` · ${filtered.length}/${catalog.total} shown` : ''}`
                                : 'Tip: paste a base URL — the gateway tries both well-known paths.'}
                    </div>
                    <div className="flex items-center space-x-2">
                        {!results && (
                            <label className="flex items-center gap-1.5 text-xs text-secondary mr-2">
                                <input
                                    type="checkbox"
                                    checked={enableImported}
                                    onChange={(e) => setEnableImported(e.target.checked)}
                                    aria-label="Enable the imported card"
                                />
                                Enabled on import
                            </label>
                        )}
                        <button
                            onClick={onClose}
                            className="px-4 py-1.5 text-sm font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt transition-colors"
                        >
                            {results ? 'Done' : 'Cancel'}
                        </button>
                        {!results && mode === 'catalog' && (
                            <button
                                onClick={asyncHandler(submitCatalog, 'click')}
                                disabled={picked.size === 0 || submitting}
                                className="flex items-center space-x-1.5 px-4 py-1.5 text-sm font-medium text-white bg-primary-500 hover:bg-primary-600 disabled:opacity-50 disabled:cursor-not-allowed rounded-lg transition-colors"
                            >
                                {submitting ? (
                                    <Loader2 className="w-4 h-4 animate-spin" />
                                ) : (
                                    <Download className="w-4 h-4" />
                                )}
                                <span>Import {picked.size > 0 ? picked.size : ''}</span>
                            </button>
                        )}
                        {!results && mode === 'url' && (
                            <button
                                onClick={asyncHandler(submitUrl, 'click')}
                                disabled={!url.trim() || submitting}
                                className="flex items-center space-x-1.5 px-4 py-1.5 text-sm font-medium text-white bg-primary-500 hover:bg-primary-600 disabled:opacity-50 disabled:cursor-not-allowed rounded-lg transition-colors"
                            >
                                {submitting ? (
                                    <Loader2 className="w-4 h-4 animate-spin" />
                                ) : (
                                    <Download className="w-4 h-4" />
                                )}
                                <span>Import</span>
                            </button>
                        )}
                    </div>
                </div>
            </div>
        </div>
    );
};

export default AgentCardImportModal;

// ─── Sub-components ─────────────────────────────────────────────────────────

const ModeTab: React.FC<{
    active: boolean;
    onClick: () => void;
    icon: React.ReactNode;
    label: string;
    sub: string;
}> = ({ active, onClick, icon, label, sub }) => (
    <button
        onClick={onClick}
        className={
            'flex items-center space-x-2 px-4 py-2.5 text-sm font-medium rounded-t-lg border-b-2 transition-colors ' +
            (active
                ? 'border-primary-500 text-primary-700 bg-primary-50'
                : 'border-transparent text-secondary hover:text-default hover:bg-surface-alt')
        }
    >
        {icon}
        <div className="flex flex-col items-start leading-tight">
            <span>{label}</span>
            <span className="text-[10px] text-muted font-mono">{sub}</span>
        </div>
    </button>
);

interface CatalogPanelProps {
    catalog: CatalogResponse | null;
    loading: boolean;
    error: string | null;
    search: string;
    onSearch: (s: string) => void;
    healthyOnly: boolean;
    onHealthyOnly: (v: boolean) => void;
    filtered: CatalogAgent[];
    picked: Set<string>;
    onToggle: (key: string) => void;
    onPickAll: () => void;
    onClear: () => void;
    onRefresh: () => void;
}

const CatalogPanel: React.FC<CatalogPanelProps> = ({
    catalog, loading, error, search, onSearch,
    healthyOnly, onHealthyOnly, filtered,
    picked, onToggle, onPickAll, onClear, onRefresh,
}) => {
    if (error) {
        return (
            <InfoBanner variant="error" title="Catalog unavailable" className="mb-0">
                <p className="text-xs">{error}</p>
                <button
                    onClick={onRefresh}
                    className="mt-2 inline-flex items-center space-x-1.5 px-2.5 py-1 text-xs font-medium text-red-700 bg-red-100 border border-red-200 rounded hover:bg-red-200"
                >
                    <RefreshCw className="w-3 h-3" />
                    <span>Retry</span>
                </button>
            </InfoBanner>
        );
    }
    if (loading || !catalog) {
        return (
            <div className="flex items-center justify-center py-16 text-secondary">
                <Loader2 className="w-5 h-5 animate-spin mr-2" />
                Fetching catalog…
            </div>
        );
    }
    return (
        <div className="space-y-3">
            <div className="flex items-center space-x-2">
                <div className="flex-1 relative min-h-0 min-w-0">
                    <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
                    <input
                        value={search}
                        onChange={e => onSearch(e.target.value)}
                        placeholder="Search by name, skill, provider…"
                        className="w-full pl-9 pr-3 py-2 text-sm rounded-lg border border-strong bg-surface text-default focus:border-primary-500 focus:ring-1 focus:ring-primary-500"
                    />
                </div>
                <label className="inline-flex items-center space-x-1.5 text-xs text-secondary whitespace-nowrap">
                    <input
                        type="checkbox"
                        checked={healthyOnly}
                        onChange={e => onHealthyOnly(e.target.checked)}
                        className="h-4 w-4 rounded border-strong text-primary-600 focus:ring-primary-500"
                    />
                    <span>Healthy only</span>
                </label>
                <button
                    onClick={onRefresh}
                    className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors"
                    title="Re-fetch catalog (bypass cache)"
                >
                    <RefreshCw className="w-4 h-4" />
                </button>
            </div>
            <div className="flex items-center justify-between text-xs text-muted">
                <span>
                    {catalog.cached ? 'Cached snapshot' : 'Live snapshot'} ·{' '}
                    {new Date(catalog.fetched_at).toLocaleTimeString()}
                </span>
                <div className="flex items-center space-x-3">
                    <button
                        onClick={onPickAll}
                        disabled={filtered.length === 0}
                        className="text-primary-600 hover:underline disabled:opacity-50"
                    >
                        Select all visible
                    </button>
                    <button
                        onClick={onClear}
                        disabled={picked.size === 0}
                        className="text-secondary hover:underline disabled:opacity-50"
                    >
                        Clear
                    </button>
                </div>
            </div>
            {filtered.length === 0 ? (
                <div className="text-center py-10 text-sm text-muted">
                    No agents match the current filters.
                </div>
            ) : (
                <ul className="divide-y divide-default border border-default rounded-lg">
                    {filtered.map(a => {
                        const key = catalogKey(a) ?? '';
                        if (!key) return null;
                        const isPicked = picked.has(key);
                        const sourceUrl = a.well_known_url || a.base_url || '';
                        return (
                            <li
                                key={key}
                                onClick={() => onToggle(key)}
                                className={
                                    'flex items-start gap-3 px-3 py-2.5 cursor-pointer transition-colors ' +
                                    (isPicked ? 'bg-primary-50' : 'hover:bg-surface-alt')
                                }
                            >
                                <input
                                    type="checkbox"
                                    checked={isPicked}
                                    readOnly
                                    className="mt-1 h-4 w-4 rounded border-strong text-primary-600 focus:ring-primary-500 pointer-events-none"
                                />
                                <CardIcon
                                    src={a.icon_url}
                                    alt={a.name ?? ''}
                                    size={36}
                                />
                                <div className="min-w-0 flex-1 min-h-0">
                                    <div className="flex items-center gap-2 flex-wrap">
                                        <span className="font-medium text-default truncate">
                                            {a.name ?? '(no name)'}
                                        </span>
                                        {a.protocol_version && (
                                            <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-surface-raised text-muted">
                                                A2A {a.protocol_version}
                                            </span>
                                        )}
                                        {a.healthy === true && (
                                            <span className="flex items-center gap-1 text-[10px] text-green-700 bg-green-50 border border-green-200 rounded px-1.5 py-0.5">
                                                <Heart className="w-3 h-3" />
                                                healthy
                                                {typeof a.uptime_pct === 'number'
                                                    ? ` · ${a.uptime_pct.toFixed(1)}%`
                                                    : ''}
                                            </span>
                                        )}
                                        {a.healthy === false && (
                                            <span className="text-[10px] text-red-700 bg-red-50 border border-red-200 rounded px-1.5 py-0.5">
                                                offline
                                            </span>
                                        )}
                                        {a.skill_count > 0 && (
                                            <span className="text-[10px] text-muted">
                                                {a.skill_count} skill{a.skill_count === 1 ? '' : 's'}
                                            </span>
                                        )}
                                    </div>
                                    {a.description && (
                                        <p className="mt-0.5 text-xs text-secondary line-clamp-2">
                                            {a.description}
                                        </p>
                                    )}
                                    <p className="mt-0.5 text-[10px] font-mono text-muted truncate">
                                        {sourceUrl || '(no URL)'}
                                    </p>
                                </div>
                            </li>
                        );
                    })}
                </ul>
            )}
        </div>
    );
};

const UrlPanel: React.FC<{
    url: string;
    onUrl: (s: string) => void;
    nameOverride: string;
    onNameOverride: (s: string) => void;
}> = ({ url, onUrl, nameOverride, onNameOverride }) => (
    <div className="space-y-3">
        <div>
            <label className="block text-xs font-medium text-secondary mb-1">
                Source URL
            </label>
            <input
                value={url}
                onChange={e => onUrl(e.target.value)}
                placeholder="https://example.com or https://example.com/.well-known/agent-card.json"
                className="w-full px-3 py-2 rounded-lg border border-strong bg-surface text-default text-sm font-mono focus:border-primary-500 focus:ring-1 focus:ring-primary-500"
            />
            <p className="text-xs text-muted mt-1">
                Paste a base URL — the gateway tries{' '}
                <code>/.well-known/agent-card.json</code> (A2A ≥ 0.3) first, then{' '}
                <code>/.well-known/agent.json</code> (legacy 0.2.x). You can also
                paste either full URL directly.
            </p>
        </div>
        <div>
            <label className="block text-xs font-medium text-secondary mb-1">
                Name override <span className="text-muted">(optional)</span>
            </label>
            <input
                value={nameOverride}
                onChange={e => onNameOverride(e.target.value)}
                placeholder="my-team-friendly-name"
                className="w-full px-3 py-2 rounded-lg border border-strong bg-surface text-default text-sm focus:border-primary-500 focus:ring-1 focus:ring-primary-500"
            />
            <p className="text-xs text-muted mt-1">
                If left blank, the imported card uses its own <code>name</code>{' '}
                field. Override only when two upstream cards collide on the
                same name in this tenant.
            </p>
        </div>
    </div>
);

const ResultList: React.FC<{
    results: BulkImportResult[];
    onAgain: () => void;
}> = ({ results, onAgain }) => (
    <div className="space-y-3">
        <ul className="divide-y divide-default border border-default rounded-lg">
            {results.map((r, idx) => (
                <li key={idx} className="flex items-start gap-3 px-3 py-2.5">
                    {r.success ? (
                        <Check className="w-5 h-5 text-green-600 flex-shrink-0 mt-0.5" />
                    ) : (
                        <XCircle className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
                    )}
                    <div className="min-w-0 flex-1 min-h-0">
                        <p className="text-sm text-default truncate">
                            {r.success ? r.name ?? r.source_url : r.source_url}
                        </p>
                        {r.error && (
                            <p className="text-xs text-red-600 break-words mt-0.5">
                                {r.error}
                            </p>
                        )}
                    </div>
                </li>
            ))}
        </ul>
        <div>
            <button
                onClick={onAgain}
                className="text-sm text-primary-600 hover:underline"
            >
                Import more
            </button>
        </div>
    </div>
);

// ─── Helpers ────────────────────────────────────────────────────────────────

/** Stable per-row key for the catalog. Falls back through the most stable
 *  fields the registry might supply so we don't lose selection state when
 *  the registry's `id` field is absent. */
function catalogKey(a: CatalogAgent): string | null {
    return a.id || a.well_known_url || a.base_url || a.name || null;
}

interface CardIconProps {
    src?: string | null;
    alt: string;
    size: number;
}

/** Renders the agent's icon, or a tinted initial fallback. Errors load
 *  silently swap to the fallback so a broken upstream image doesn't
 *  leave the row blank. */
export const CardIcon: React.FC<CardIconProps> = ({ src, alt, size }) => {
    const [errored, setErrored] = useState(false);
    if (src && !errored) {
        return (
            <img
                src={src}
                alt={alt}
                width={size}
                height={size}
                onError={() => setErrored(true)}
                className="rounded-md object-cover bg-surface-raised border border-default flex-shrink-0"
                style={{ width: size, height: size }}
            />
        );
    }
    const initial = (alt || '?').trim().charAt(0).toUpperCase() || '?';
    return (
        <div
            className="rounded-md bg-primary-100 text-primary-700 flex items-center justify-center font-semibold flex-shrink-0"
            style={{ width: size, height: size, fontSize: size * 0.45 }}
            aria-label={alt}
        >
            {initial}
        </div>
    );
};

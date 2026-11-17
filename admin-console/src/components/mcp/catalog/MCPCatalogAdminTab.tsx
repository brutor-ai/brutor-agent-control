/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * System-Admin → MCP Catalog page.
 *
 * The global MCP catalog is the platform-wide source of truth for the MCP
 * servers tenants can browse and import. Tenant admins browse + import
 * entries (and may add their own per-tenant custom entries); system admins
 * CURATE the GLOBAL catalog here — create global system entries, edit
 * metadata, and delete entries.
 *
 * Sysadmin-only (the nav item carries `requiresSystemAdmin`). Created /
 * updated / deleted entries are GLOBAL (tenant_id NULL, source='system').
 * Data fetching is hand-rolled (useState/useEffect/useCallback) — NO
 * react-query — mirroring the LLM Catalog page conventions.
 */

import React, { useCallback, useEffect, useState } from 'react';
import { TABLE_TH_TYPE } from '../../ui/tableStyles';
import { Boxes, Loader2, Pencil, Plus, Search, Trash2, X } from 'lucide-react';

import { useGateway } from '../../../hooks/useGateway';
import { useToast } from '../../common/Toast';
import { useConfirm } from '../../common/ConfirmationDialog';
import InfoBanner from '../../common/InfoBanner';
import type {
    MCPCatalogEntry,
    MCPCatalogEntryBrief,
    CatalogEntryListResponse,
} from '../../../lib/mcpCatalogTypes';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';

const PAGE_LIMIT = 100;

const RUNTIMES = ['remote', 'containerized'] as const;
const AUTH_PATTERNS = ['none', 'oauth', 'api_key', 'header'] as const;

const TH =
    `px-6 py-3 text-left ${TABLE_TH_TYPE}`;

const INPUT_CLASS =
    'w-full px-3 py-2 rounded-lg border border-strong bg-surface text-sm text-default focus:outline-none focus:ring-2 focus:ring-primary-500';
const LABEL_CLASS = 'block text-xs font-medium text-secondary mb-1';

const filterSelect =
    'px-3 py-2 rounded-lg border border-strong bg-surface text-sm text-default focus:outline-none focus:ring-2 focus:ring-primary-500';

// ---------------------------------------------------------------------------
// Create / edit dialog
// ---------------------------------------------------------------------------

interface DialogProps {
    mode: 'create' | 'edit';
    initial: MCPCatalogEntryBrief | MCPCatalogEntry | null;
    onClose: () => void;
    onSaved: () => void | Promise<void>;
}

function csvToList(v: string): string[] {
    return v
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean);
}

const MCPCatalogEditDialog: React.FC<DialogProps> = ({
    mode,
    initial,
    onClose,
    onSaved,
}) => {
    const { gatewayClient } = useGateway();
    const toast = useToast();

    const isCreate = mode === 'create';

    const [name, setName] = useState(initial?.name ?? '');
    const [slug, setSlug] = useState(initial?.slug ?? '');
    const [shortDescription, setShortDescription] = useState(
        initial?.short_description ?? '',
    );
    const [description, setDescription] = useState(
        (initial as MCPCatalogEntry | null)?.description ?? '',
    );
    const [runtime, setRuntime] = useState<string>(initial?.runtime ?? 'remote');
    const [authPattern, setAuthPattern] = useState<string>(
        initial?.auth_pattern ?? 'none',
    );
    const [iconUrl, setIconUrl] = useState(initial?.icon_url ?? '');
    const [categories, setCategories] = useState(
        (initial?.categories ?? []).join(', '),
    );
    // Remote: fixedURL. Containerized: image.
    const initRemote = (initial as MCPCatalogEntry | null)?.remote_config;
    const initContainer = (initial as MCPCatalogEntry | null)?.containerized_config;
    const [fixedUrl, setFixedUrl] = useState(initRemote?.fixedURL ?? '');
    const [image, setImage] = useState(initContainer?.image ?? '');
    const [enabled, setEnabled] = useState(initial?.enabled ?? true);

    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);

    async function handleSave() {
        setError(null);
        if (!name.trim()) {
            setError('Name is required.');
            return;
        }
        if (isCreate && !slug.trim()) {
            setError('Slug is required.');
            return;
        }

        const remoteConfig =
            runtime === 'remote' && fixedUrl.trim()
                ? { fixedURL: fixedUrl.trim() }
                : undefined;
        const containerizedConfig =
            runtime === 'containerized' && image.trim()
                ? { image: image.trim(), port: 8099, path: '/' }
                : undefined;

        setSaving(true);
        try {
            if (isCreate) {
                await gatewayClient.createGlobalMCPCatalogEntry({
                    name: name.trim(),
                    slug: slug.trim(),
                    short_description: shortDescription.trim() || null,
                    description: description.trim() || null,
                    runtime,
                    auth_pattern: authPattern,
                    icon_url: iconUrl.trim() || null,
                    categories: csvToList(categories),
                    remote_config: remoteConfig,
                    containerized_config: containerizedConfig,
                    enabled,
                });
                toast.success('Global catalog entry created');
            } else if (initial) {
                await gatewayClient.updateGlobalMCPCatalogEntry(initial.id, {
                    name: name.trim(),
                    short_description: shortDescription.trim() || null,
                    description: description.trim() || null,
                    runtime,
                    auth_pattern: authPattern,
                    icon_url: iconUrl.trim() || null,
                    categories: csvToList(categories),
                    remote_config: remoteConfig,
                    containerized_config: containerizedConfig,
                    enabled,
                });
                toast.success('Global catalog entry updated');
            }
            await onSaved();
            onClose();
        } catch (e: unknown) {
            const msg = e instanceof Error ? e.message : String(e);
            setError(msg);
            toast.error(isCreate ? 'Create failed' : 'Save failed', msg);
        } finally {
            setSaving(false);
        }
    }

    return (
        <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
            <div className="bg-surface rounded-xl shadow-2xl w-full max-w-2xl max-h-[90vh] flex flex-col">
                <div className="flex items-center justify-between border-b border-default px-5 py-4">
                    <h2 className="text-lg font-semibold text-default">
                        {isCreate
                            ? 'Add global catalog entry'
                            : 'Edit global catalog entry'}
                    </h2>
                    <button
                        onClick={onClose}
                        disabled={saving}
                        className="p-1.5 hover:bg-surface-alt rounded-md transition-colors disabled:opacity-50"
                        aria-label="Close"
                    >
                        <X className="w-5 h-5 text-muted" />
                    </button>
                </div>

                <div className="scroll-region p-5 space-y-4">
                    {error && (
                        <InfoBanner variant="error" className="mb-0">
                            <p>{error}</p>
                        </InfoBanner>
                    )}

                    <div className="grid grid-cols-2 gap-4">
                        <div>
                            <label className={LABEL_CLASS}>
                                Name <span className="text-red-500">*</span>
                            </label>
                            <input
                                type="text"
                                value={name}
                                onChange={(e) => setName(e.target.value)}
                                placeholder="e.g. GitHub"
                                className={INPUT_CLASS}
                            />
                        </div>
                        <div>
                            <label className={LABEL_CLASS}>
                                Slug{' '}
                                {isCreate && <span className="text-red-500">*</span>}
                            </label>
                            {isCreate ? (
                                <input
                                    type="text"
                                    value={slug}
                                    onChange={(e) => setSlug(e.target.value)}
                                    placeholder="e.g. github"
                                    className={INPUT_CLASS}
                                />
                            ) : (
                                <p className="px-3 py-2 text-sm text-default bg-surface-alt rounded-lg border border-default font-mono truncate">
                                    {slug}
                                </p>
                            )}
                        </div>
                    </div>

                    <div>
                        <label className={LABEL_CLASS}>Short description</label>
                        <input
                            type="text"
                            value={shortDescription}
                            onChange={(e) => setShortDescription(e.target.value)}
                            placeholder="One-line summary"
                            className={INPUT_CLASS}
                        />
                    </div>

                    <div>
                        <label className={LABEL_CLASS}>Description</label>
                        <textarea
                            value={description}
                            onChange={(e) => setDescription(e.target.value)}
                            rows={2}
                            placeholder="Full markdown description"
                            className={INPUT_CLASS}
                        />
                    </div>

                    <div className="grid grid-cols-2 gap-4">
                        <div>
                            <label className={LABEL_CLASS}>Runtime</label>
                            <select
                                value={runtime}
                                onChange={(e) => setRuntime(e.target.value)}
                                className={INPUT_CLASS}
                                aria-label="Runtime"
                            >
                                {RUNTIMES.map((r) => (
                                    <option key={r} value={r}>
                                        {r}
                                    </option>
                                ))}
                            </select>
                        </div>
                        <div>
                            <label className={LABEL_CLASS}>Auth pattern</label>
                            <select
                                value={authPattern}
                                onChange={(e) => setAuthPattern(e.target.value)}
                                className={INPUT_CLASS}
                                aria-label="Auth pattern"
                            >
                                {AUTH_PATTERNS.map((a) => (
                                    <option key={a} value={a}>
                                        {a}
                                    </option>
                                ))}
                            </select>
                        </div>
                    </div>

                    {runtime === 'remote' ? (
                        <div>
                            <label className={LABEL_CLASS}>Fixed URL</label>
                            <input
                                type="text"
                                value={fixedUrl}
                                onChange={(e) => setFixedUrl(e.target.value)}
                                placeholder="https://api.example.com/mcp"
                                className={INPUT_CLASS}
                            />
                        </div>
                    ) : (
                        <div>
                            <label className={LABEL_CLASS}>Container image</label>
                            <input
                                type="text"
                                value={image}
                                onChange={(e) => setImage(e.target.value)}
                                placeholder="ghcr.io/org/image:tag"
                                className={INPUT_CLASS}
                            />
                        </div>
                    )}

                    <div className="grid grid-cols-2 gap-4">
                        <div>
                            <label className={LABEL_CLASS}>Icon URL</label>
                            <input
                                type="text"
                                value={iconUrl}
                                onChange={(e) => setIconUrl(e.target.value)}
                                placeholder="https://…/icon.svg"
                                className={INPUT_CLASS}
                            />
                        </div>
                        <div>
                            <label className={LABEL_CLASS}>
                                Categories (comma-separated)
                            </label>
                            <input
                                type="text"
                                value={categories}
                                onChange={(e) => setCategories(e.target.value)}
                                placeholder="Business, Communication"
                                className={INPUT_CLASS}
                            />
                        </div>
                    </div>

                    <div className="flex items-center gap-6">
                        <label className="inline-flex items-center gap-2 text-sm text-default">
                            <input
                                type="checkbox"
                                checked={enabled}
                                onChange={(e) => setEnabled(e.target.checked)}
                                className="rounded border-strong"
                            />
                            Enabled
                        </label>
                    </div>
                </div>

                <div className="flex justify-end gap-3 border-t border-default px-5 py-4">
                    <button
                        onClick={onClose}
                        disabled={saving}
                        className="px-4 py-2 text-sm text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50"
                    >
                        Cancel
                    </button>
                    <button
                        onClick={asyncHandler(handleSave, 'click')}
                        disabled={saving}
                        className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-primary-500 hover:bg-primary-600 rounded-lg transition-colors disabled:opacity-50"
                    >
                        {saving && <Loader2 className="w-4 h-4 animate-spin" />}
                        {isCreate ? 'Create entry' : 'Save changes'}
                    </button>
                </div>
            </div>
        </div>
    );
};

// ---------------------------------------------------------------------------
// Main tab
// ---------------------------------------------------------------------------

const MCPCatalogAdminTab: React.FC = () => {
    const { gatewayClient } = useGateway();
    const toast = useToast();
    const confirm = useConfirm();

    const [entries, setEntries] = useState<MCPCatalogEntryBrief[]>([]);
    const [total, setTotal] = useState(0);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    // Filters
    const [runtime, setRuntime] = useState('');
    const [search, setSearch] = useState('');
    const [offset, setOffset] = useState(0);

    // Dialog
    const [creating, setCreating] = useState(false);
    const [editing, setEditing] = useState<MCPCatalogEntryBrief | null>(null);

    const refresh = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            // Browse is global ∪ own; as a sysadmin (no home tenant) this
            // surfaces the GLOBAL system entries. Filter to source=system so
            // the curation table only shows entries the sysadmin owns.
            const res = (await gatewayClient.fetchMCPCatalogEntries({
                runtime: runtime || undefined,
                search: search.trim() || undefined,
                source: 'system',
                limit: PAGE_LIMIT,
                offset,
            })) as CatalogEntryListResponse;
            setEntries(res.entries);
            setTotal(res.total);
        } catch (e: unknown) {
            const msg = e instanceof Error ? e.message : String(e);
            setError(msg);
        } finally {
            setLoading(false);
        }
    }, [gatewayClient, runtime, search, offset]);

    useEffect(() => {
        runBackground(refresh(), 'refresh');
    }, [refresh]);

    async function handleDelete(entry: MCPCatalogEntryBrief) {
        const ok = await confirm({
            title: 'Delete global catalog entry',
            message: `Delete "${entry.name}" from the global MCP catalog? Tenants will no longer be able to import it. Already-imported servers are unaffected.`,
            confirmLabel: 'Delete',
            variant: 'danger',
        });
        if (!ok) return;
        try {
            await gatewayClient.deleteGlobalMCPCatalogEntry(entry.id);
            toast.success('Global catalog entry deleted');
            await refresh();
        } catch (e: unknown) {
            const msg = e instanceof Error ? e.message : String(e);
            toast.error('Delete failed', msg);
        }
    }

    function resetFilters() {
        setRuntime('');
        setSearch('');
        setOffset(0);
    }

    const page = Math.floor(offset / PAGE_LIMIT) + 1;
    const pageCount = Math.max(1, Math.ceil(total / PAGE_LIMIT));

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
                            MCP Catalog
                        </h1>
                        <p className="text-sm text-muted">
                            Curate the global catalog of MCP servers tenants can
                            browse and import.
                        </p>
                    </div>
                </div>

                <button
                    onClick={() => setCreating(true)}
                    className="flex items-center space-x-2 px-4 py-2 text-sm font-medium text-white bg-primary-500 hover:bg-primary-600 rounded-lg transition-colors"
                >
                    <Plus className="w-4 h-4" />
                    <span>Add entry</span>
                </button>
            </div>

            <InfoBanner title="What the global MCP catalog is">
                <p>
                    The catalog is the platform-wide source of truth for the MCP
                    servers every tenant can browse and import. Tenant admins
                    import an entry to create a configured MCP server in their
                    tenant. As a system admin you curate the{' '}
                    <strong>global</strong> catalog here — add system entries, edit
                    metadata, and delete entries. Deleting an entry does not affect
                    servers a tenant has already imported.
                </p>
            </InfoBanner>

            {/* Filters */}
            <div className="flex flex-wrap items-center gap-3">
                <select
                    value={runtime}
                    onChange={(e) => {
                        setRuntime(e.target.value);
                        setOffset(0);
                    }}
                    className={filterSelect}
                    aria-label="Filter by runtime"
                >
                    <option value="">All runtimes</option>
                    {RUNTIMES.map((r) => (
                        <option key={r} value={r}>
                            {r}
                        </option>
                    ))}
                </select>

                <div className="relative flex-1 min-w-[200px] min-h-0 min-w-0">
                    <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted pointer-events-none" />
                    <input
                        type="text"
                        value={search}
                        onChange={(e) => setSearch(e.target.value)}
                        onBlur={() => setOffset(0)}
                        onKeyDown={(e) => {
                            if (e.key === 'Enter') setOffset(0);
                        }}
                        placeholder="Search name / description…"
                        className="w-full pl-9 pr-3 py-2 rounded-lg border border-strong bg-surface text-sm text-default focus:outline-none focus:ring-2 focus:ring-primary-500"
                        aria-label="Search catalog"
                    />
                </div>

                <button
                    onClick={resetFilters}
                    className="px-4 py-2 text-sm font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt transition-colors"
                >
                    Reset
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
            ) : entries.length === 0 ? (
                <div className="text-center py-16 bg-surface rounded-xl border-2 border-dashed border-default">
                    <Boxes className="w-10 h-10 text-muted mx-auto mb-3" />
                    <h3 className="text-base font-semibold text-default mb-1">
                        No catalog entries
                    </h3>
                    <p className="text-sm text-muted max-w-md mx-auto">
                        Click <strong>Add entry</strong> to create a global catalog
                        entry.
                    </p>
                </div>
            ) : (
                <div className="bg-surface border border-default rounded-lg shadow-sm overflow-hidden">
                    <div className="overflow-x-auto">
                        <table className="min-w-full divide-y divide-default">
                            <thead className="bg-surface-alt">
                                <tr>
                                    <th className={TH}>Name</th>
                                    <th className={TH}>Slug</th>
                                    <th className={TH}>Runtime</th>
                                    <th className={TH}>Auth</th>
                                    <th className={TH}>Status</th>
                                    <th className={`${TH} text-right`}>Actions</th>
                                </tr>
                            </thead>
                            <tbody className="bg-surface divide-y divide-default">
                                {entries.map((e) => (
                                    <tr
                                        key={e.id}
                                        className="hover:bg-surface-alt transition-colors"
                                    >
                                        <td className="px-6 py-4">
                                            <div className="flex items-center gap-3 min-w-0">
                                                {e.icon_url && (
                                                    <img
                                                        src={e.icon_url}
                                                        alt={`${e.name} icon`}
                                                        className="w-5 h-5 object-contain flex-shrink-0"
                                                    />
                                                )}
                                                <div className="min-w-0">
                                                    <div className="font-medium text-default truncate max-w-xs">
                                                        {e.name}
                                                    </div>
                                                    {e.short_description && (
                                                        <div className="text-xs text-muted truncate max-w-xs">
                                                            {e.short_description}
                                                        </div>
                                                    )}
                                                </div>
                                            </div>
                                        </td>
                                        <td className="px-6 py-4 whitespace-nowrap text-xs font-mono text-secondary">
                                            {e.slug}
                                        </td>
                                        <td className="px-6 py-4 whitespace-nowrap">
                                            <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-surface-raised text-secondary border border-default">
                                                {e.runtime}
                                            </span>
                                        </td>
                                        <td className="px-6 py-4 whitespace-nowrap text-sm text-secondary">
                                            {e.auth_pattern}
                                        </td>
                                        <td className="px-6 py-4 whitespace-nowrap">
                                            {e.enabled ? (
                                                <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-green-50 text-green-700 border border-green-200">
                                                    Enabled
                                                </span>
                                            ) : (
                                                <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-surface-raised text-muted border border-default">
                                                    Disabled
                                                </span>
                                            )}
                                        </td>
                                        <td className="px-6 py-4 whitespace-nowrap text-right">
                                            <div className="inline-flex items-center gap-1">
                                                <button
                                                    onClick={() => setEditing(e)}
                                                    className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors"
                                                    title="Edit entry"
                                                    aria-label={`Edit ${e.name}`}
                                                >
                                                    <Pencil className="w-4 h-4" />
                                                </button>
                                                <button
                                                    onClick={asyncHandler(() => handleDelete(e), 'click')}
                                                    className="p-2 text-muted hover:text-red-600 hover:bg-red-50 rounded-lg transition-colors"
                                                    title="Delete entry"
                                                    aria-label={`Delete ${e.name}`}
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
                            {total} entr{total === 1 ? 'y' : 'ies'}
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

            {creating && (
                <MCPCatalogEditDialog
                    mode="create"
                    initial={null}
                    onClose={() => setCreating(false)}
                    onSaved={refresh}
                />
            )}

            {editing && (
                <MCPCatalogEditDialog
                    mode="edit"
                    initial={editing}
                    onClose={() => setEditing(null)}
                    onSaved={refresh}
                />
            )}
        </div>
    );
};

export default MCPCatalogAdminTab;

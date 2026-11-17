/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Resources | AI Models | Providers admin tab.
 *
 * Providers are first-class DB entities — every other
 * surface (LLModelForm, portal model picker) reads them at runtime
 * via /v1/admin/providers, so this page is the single source of
 * truth for the operator.
 *
 * What lives here:
 *   * Catalogue table — icon, slug, display name, category, auth,
 *     model count, enabled flag, system badge.
 *   * "New Provider" → LLProviderForm in create mode.
 *   * Per-row Edit / Disable / Delete actions.
 *   * Delete is a no-op for is_system rows or providers still
 *     referenced by models (server enforces, UI gates).
 */

import React, { useEffect, useMemo, useState } from 'react';
import toast from 'react-hot-toast';
import {
    Plus,
    RefreshCw,
    Search,
    Edit,
    Trash2,
    Power,
    Shield,
    AlertCircle,
    ExternalLink,
} from 'lucide-react';

import { useGateway } from '../../../hooks/useGateway';
import { useLlmProviders } from '../../../hooks/useLlmProviders';
import type { LlmProviderResponse } from '../../../lib/llmProviderTypes';
import ProviderIcon from '../config/ProviderIcon';
import LLProviderForm from './LLProviderForm';
import ProviderModelsView from './ProviderModelsView';
import GenericDeleteConfirmationModal from '../../common/GenericDeleteConfirmationModal';
import { TABLE_TH_TYPE, TABLE_THEAD, TABLE_TBODY } from '../../ui/tableStyles';
import { errorMessage } from '../../../lib/narrow';

const CATEGORY_LABEL: Record<string, string> = {
    chat: 'Chat / Reasoning',
    image: 'Image generation',
    audio: 'Audio (TTS / STT)',
    video: 'Video generation',
    embeddings: 'Embeddings',
    self_hosted: 'Self-hosted / OSS',
    internal: 'Internal',
};

const AUTH_LABEL: Record<string, string> = {
    api_key: 'API key',
    oauth2: 'OAuth 2.0',
    gcp_service_account: 'GCP service account',
    aws_iam: 'AWS IAM',
    azure_ad: 'Azure AD',
    none: 'None',
};

const ProvidersTab: React.FC = () => {
    const { gatewayClient } = useGateway();
    const {
        providers,
        loading,
        error,
        refresh,
    } = useLlmProviders(gatewayClient, { includeModelCount: true });

    const [search, setSearch] = useState('');
    const [editing, setEditing] = useState<LlmProviderResponse | null>(null);
    const [creating, setCreating] = useState(false);
    // When non-null, the right pane swaps to the drill-down view
    // showing every LLM model attached to the selected provider.
    const [drilldown, setDrilldown] = useState<LlmProviderResponse | null>(null);

    const [deleteTarget, setDeleteTarget] = useState<LlmProviderResponse | null>(null);
    const [deleting, setDeleting] = useState(false);
    const [deleteError, setDeleteError] = useState<string | null>(null);

    const [togglingId, setTogglingId] = useState<string | null>(null);

    // The hook only fetches once per (client, opts) pair within its
    // 60s SWR window — refresh() is the explicit invalidator we call
    // after a create / edit / delete so the table reflects writes
    // without waiting for the cache to expire.
    useEffect(() => {
        if (error) {
            toast.error(`Failed to load providers: ${error.message}`);
        }
    }, [error]);

    const filtered = useMemo(() => {
        const q = search.trim().toLowerCase();
        const list = q
            ? providers.filter(p =>
                p.key.toLowerCase().includes(q)
                || p.display_name.toLowerCase().includes(q)
                || (p.category ?? '').toLowerCase().includes(q),
            )
            : providers.slice();
        // Stable order: enabled first, then alpha by display name.
        list.sort((a, b) => {
            if (a.enabled !== b.enabled) return a.enabled ? -1 : 1;
            return a.display_name.localeCompare(b.display_name);
        });
        return list;
    }, [providers, search]);

    const handleToggleEnabled = async (provider: LlmProviderResponse) => {
        setTogglingId(provider.id);
        try {
            await gatewayClient.updateProvider(provider.id, {
                enabled: !provider.enabled,
            });
            toast.success(
                provider.enabled
                    ? `Disabled ${provider.display_name}`
                    : `Enabled ${provider.display_name}`,
            );
            await refresh();
        } catch (err) {
            toast.error(
                `Failed to ${provider.enabled ? 'disable' : 'enable'} provider: ${errorMessage(err) ?? err}`,
            );
        } finally {
            setTogglingId(null);
        }
    };

    const handleDelete = async () => {
        if (!deleteTarget) return;
        setDeleting(true);
        setDeleteError(null);
        try {
            await gatewayClient.deleteProvider(deleteTarget.id);
            toast.success(`Deleted provider ${deleteTarget.display_name}`);
            setDeleteTarget(null);
            await refresh();
        } catch (err) {
            // The server returns a 409 with a friendly message when
            // models still reference this provider — surface that
            // verbatim so the operator knows what to reassign.
            const message = errorMessage(err) ?? String(err);
            setDeleteError(message);
        } finally {
            setDeleting(false);
        }
    };

    const handleSaved = async (msg: string) => {
        toast.success(msg);
        setEditing(null);
        setCreating(false);
        await refresh();
    };

    // Keep the drill-down's provider object fresh after edits / icon
    // uploads — the array we filter from is the always-current
    // `providers` list, but `drilldown` was a snapshot at click time.
    useEffect(() => {
        if (!drilldown) return;
        const fresh = providers.find(p => p.id === drilldown.id);
        if (fresh && fresh !== drilldown) {
            setDrilldown(fresh);
        }
    }, [providers, drilldown]);

    // Drill-down render — full-width swap of the list. Modals (form,
    // delete) still render in the parent so editing the provider from
    // the drill-down header works the same way.
    if (drilldown) {
        return (
            <>
                <ProviderModelsView
                    provider={drilldown}
                    onBack={() => setDrilldown(null)}
                    onEditProvider={p => setEditing(p)}
                />
                {(creating || editing) && (
                    <LLProviderForm
                        provider={editing}
                        onClose={() => {
                            setEditing(null);
                            setCreating(false);
                        }}
                        onSaved={handleSaved}
                    />
                )}
            </>
        );
    }

    return (
        <div className="app-column h-full">
            {/* Toolbar */}
            <div className="flex-shrink-0 px-6 py-4 bg-surface border-b border-default flex items-center gap-3">
                <div className="relative flex-1 max-w-md min-h-0 min-w-0">
                    <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted" />
                    <input
                        type="text"
                        value={search}
                        onChange={e => setSearch(e.target.value)}
                        placeholder="Search providers (key, name, category)…"
                        className="w-full pl-9 pr-3 py-2 text-sm bg-surface-alt border border-default rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                    />
                </div>
                <button
                    onClick={() => void refresh()}
                    disabled={loading}
                    className="flex items-center space-x-1.5 px-3 py-2 text-sm font-medium text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50"
                    title="Refresh"
                >
                    <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
                    <span>Refresh</span>
                </button>
                <button
                    onClick={() => setCreating(true)}
                    className="flex items-center space-x-1.5 px-3 py-2 text-sm font-medium text-white bg-primary-600 hover:bg-primary-700 rounded-lg transition-colors"
                >
                    <Plus className="w-4 h-4" />
                    <span>New Provider</span>
                </button>
            </div>

            {/* Body */}
            <div className="flex-1 overflow-auto px-6 py-4 min-h-0 min-w-0">
                {loading && providers.length === 0 ? (
                    <div className="flex items-center justify-center h-32 text-muted">
                        <RefreshCw className="w-5 h-5 animate-spin mr-2" />
                        Loading providers…
                    </div>
                ) : filtered.length === 0 ? (
                    <div className="flex flex-col items-center justify-center h-48 text-muted">
                        <AlertCircle className="w-8 h-8 mb-2" />
                        <p className="text-sm">
                            {search
                                ? 'No providers match your search.'
                                : 'No providers configured yet. Click "New Provider" to add one.'}
                        </p>
                    </div>
                ) : (
                    <div className="bg-surface border border-default rounded-lg overflow-hidden">
                        <table className="w-full text-sm">
                            <thead className={TABLE_THEAD}>
                                <tr className="text-left">
                                    <th className={`px-4 py-2 w-8 ${TABLE_TH_TYPE}`} />
                                    <th className={`px-4 py-2 ${TABLE_TH_TYPE}`}>Provider</th>
                                    <th className={`px-4 py-2 ${TABLE_TH_TYPE}`}>Slug</th>
                                    <th className={`px-4 py-2 ${TABLE_TH_TYPE}`}>Category</th>
                                    <th className={`px-4 py-2 ${TABLE_TH_TYPE}`}>Auth</th>
                                    <th className={`px-4 py-2 text-right ${TABLE_TH_TYPE}`}>Models</th>
                                    <th className={`px-4 py-2 ${TABLE_TH_TYPE}`}>Status</th>
                                    <th className={`px-4 py-2 text-right ${TABLE_TH_TYPE}`}>Actions</th>
                                </tr>
                            </thead>
                            <tbody className={TABLE_TBODY}>
                                {filtered.map(p => {
                                    const inUse = (p.model_count ?? 0) > 0;
                                    const canDelete = !p.is_system && !inUse;
                                    return (
                                        <tr
                                            key={p.id}
                                            className="hover:bg-surface-alt transition-colors cursor-pointer"
                                            data-provider-id={p.id}
                                            onClick={() => setDrilldown(p)}
                                            title="View this provider's models"
                                        >
                                            <td className="px-4 py-2.5">
                                                <ProviderIcon
                                                    provider={p}
                                                    api={gatewayClient}
                                                    size={28}
                                                />
                                            </td>
                                            <td className="px-4 py-2.5">
                                                <div className="flex items-center gap-2">
                                                    <span className="font-medium text-default">
                                                        {p.display_name}
                                                    </span>
                                                    {p.is_system && (
                                                        <span
                                                            title="System provider — cannot be deleted, but can be edited or disabled."
                                                            className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide bg-blue-50 text-blue-700 border border-blue-200 rounded"
                                                        >
                                                            <Shield className="w-3 h-3" />
                                                            System
                                                        </span>
                                                    )}
                                                </div>
                                                {p.developer_url && (
                                                    <a
                                                        href={p.developer_url}
                                                        target="_blank"
                                                        rel="noreferrer"
                                                        onClick={e => e.stopPropagation()}
                                                        className="inline-flex items-center gap-1 text-xs text-primary-600 hover:underline mt-0.5"
                                                    >
                                                        Developer site
                                                        <ExternalLink className="w-3 h-3" />
                                                    </a>
                                                )}
                                            </td>
                                            <td className="px-4 py-2.5">
                                                <code className="text-xs px-1.5 py-0.5 bg-surface-alt border border-default rounded">
                                                    {p.key}
                                                </code>
                                            </td>
                                            <td className="px-4 py-2.5 text-secondary">
                                                {p.category
                                                    ? (CATEGORY_LABEL[p.category] ?? p.category)
                                                    : '—'}
                                            </td>
                                            <td className="px-4 py-2.5 text-secondary">
                                                {p.default_auth_type
                                                    ? (AUTH_LABEL[p.default_auth_type] ?? p.default_auth_type)
                                                    : '—'}
                                            </td>
                                            <td className="px-4 py-2.5 text-right tabular-nums text-secondary">
                                                {p.model_count ?? 0}
                                            </td>
                                            <td className="px-4 py-2.5">
                                                {p.enabled ? (
                                                    <span className="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium bg-green-50 text-green-700 border border-green-200 rounded">
                                                        Enabled
                                                    </span>
                                                ) : (
                                                    <span className="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium bg-gray-50 text-gray-600 border border-gray-200 rounded">
                                                        Disabled
                                                    </span>
                                                )}
                                            </td>
                                            <td
                                                className="px-4 py-2.5"
                                                onClick={e => e.stopPropagation()}
                                            >
                                                <div className="flex items-center justify-end gap-1">
                                                    <button
                                                        onClick={() => setEditing(p)}
                                                        className="p-1.5 text-secondary hover:text-default hover:bg-surface-raised rounded transition-colors"
                                                        title="Edit"
                                                    >
                                                        <Edit className="w-4 h-4" />
                                                    </button>
                                                    <button
                                                        onClick={() => void handleToggleEnabled(p)}
                                                        disabled={togglingId === p.id}
                                                        className="p-1.5 text-secondary hover:text-default hover:bg-surface-raised rounded transition-colors disabled:opacity-50"
                                                        title={p.enabled ? 'Disable' : 'Enable'}
                                                    >
                                                        <Power
                                                            className={`w-4 h-4 ${p.enabled ? 'text-green-600' : 'text-gray-400'}`}
                                                        />
                                                    </button>
                                                    <button
                                                        onClick={() => {
                                                            setDeleteError(null);
                                                            setDeleteTarget(p);
                                                        }}
                                                        disabled={!canDelete}
                                                        className="p-1.5 text-red-600 hover:bg-red-50 rounded transition-colors disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-transparent"
                                                        title={
                                                            p.is_system
                                                                ? 'System providers cannot be deleted'
                                                                : inUse
                                                                    ? `In use by ${p.model_count} model(s) — reassign first`
                                                                    : 'Delete'
                                                        }
                                                    >
                                                        <Trash2 className="w-4 h-4" />
                                                    </button>
                                                </div>
                                            </td>
                                        </tr>
                                    );
                                })}
                            </tbody>
                        </table>
                    </div>
                )}
            </div>

            {/* Create / edit modal */}
            {(creating || editing) && (
                <LLProviderForm
                    provider={editing}
                    onClose={() => {
                        setEditing(null);
                        setCreating(false);
                    }}
                    onSaved={handleSaved}
                />
            )}

            {/* Delete confirmation */}
            {deleteTarget && (
                <GenericDeleteConfirmationModal
                    isOpen={!!deleteTarget}
                    onClose={() => {
                        setDeleteTarget(null);
                        setDeleteError(null);
                    }}
                    onConfirm={() => void handleDelete()}
                    itemType="config"
                    itemName={deleteTarget.display_name}
                    itemInfo={{
                        id: deleteTarget.id,
                        description: `Provider slug: ${deleteTarget.key}`,
                    }}
                    isDeleting={deleting}
                    errorMessage={deleteError}
                    customWarning="You are about to permanently delete this provider"
                    customNote="This will fail if any LLM model still references this provider. Reassign or remove those models first."
                />
            )}
        </div>
    );
};

export default ProvidersTab;

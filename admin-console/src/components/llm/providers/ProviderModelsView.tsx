/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Drill-down view for a single provider in the Providers admin tab.
 *
 * Reached by clicking a row in `ProvidersTab`. Shows the provider
 * header (icon, slug, display name, edit shortcut) plus a flat list
 * of every LLM model attached to this provider — enabled or not —
 * with mode, status, health, and per-row actions.
 *
 * Actions mirror the Configuration tab so an operator can manage the
 * model end-to-end without leaving the Providers context: Edit (same
 * `LLModelForm` modal as Resources → AI Models → Configuration),
 * Enable / Disable, Copy (server-side duplication that preserves the
 * encrypted API key), and Delete (with the shared confirmation
 * modal). The list is filtered client-side from `listLLMs()` because
 * the catalogue is small (typically <50 rows per tenant) and we get
 * the full payload for free from the existing API.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import toast from 'react-hot-toast';
import {
    ArrowLeft,
    RefreshCw,
    Edit,
    AlertCircle,
    ExternalLink,
    Activity,
    Power,
    Copy,
    Trash2,
    MoreVertical,
} from 'lucide-react';

import { useGateway } from '../../../hooks/useGateway';
import type { LlmProviderResponse } from '../../../lib/llmProviderTypes';
import type {
    LLModelConfig,
    } from '../../../lib/llmTypes';
import { formatCostByMode } from '../../../lib/modelCostFormatting';
import ProviderIcon from '../config/ProviderIcon';
import LLModelForm, { type LLModelFormData } from '../config/LLModelForm';
import GenericDeleteConfirmationModal from '../../common/GenericDeleteConfirmationModal';
import { TABLE_TH_TYPE, TABLE_THEAD, TABLE_TBODY } from '../../ui/tableStyles';
import { errorMessage } from '../../../lib/narrow';
import { asyncHandler } from '../../../lib/asyncSafe';

interface Props {
    provider: LlmProviderResponse;
    onBack: () => void;
    onEditProvider: (provider: LlmProviderResponse) => void;
    /** Optional deep-link callback. When given, Edit routes into the
     *  Configuration tab; without it the drill-down opens the same
     *  edit form locally. */
    onOpenModel?: (modelId: string) => void;
}

const HEALTH_BADGE: Record<string, { label: string; className: string }> = {
    healthy: { label: 'Healthy', className: 'bg-green-50 text-green-700 border-green-200' },
    unhealthy: { label: 'Unhealthy', className: 'bg-red-50 text-red-700 border-red-200' },
    degraded: { label: 'Degraded', className: 'bg-yellow-50 text-yellow-700 border-yellow-200' },
    requires_tier: { label: 'Tier required', className: 'bg-amber-50 text-amber-700 border-amber-200' },
    unknown: { label: 'Unknown', className: 'bg-gray-50 text-gray-600 border-gray-200' },
};

const MODE_LABEL: Record<string, string> = {
    chat: 'Chat',
    completion: 'Completion',
    embedding: 'Embedding',
    image: 'Image',
    image_generation: 'Image',
    audio_tts: 'TTS',
    audio_speech: 'TTS',
    audio_stt: 'STT',
    audio_transcription: 'STT',
    video: 'Video',
    video_generation: 'Video',
    routing: 'Routing',
};

const modeLabelFor = (mode: string | undefined): string => {
    if (!mode) return '—';
    return MODE_LABEL[mode] ?? mode;
};

type SortKey = 'name' | 'model' | 'mode' | 'status' | 'health';
type SortDir = 'asc' | 'desc';
type StatusFilter = 'all' | 'enabled' | 'disabled';

interface RowActionsMenuProps {
    model: LLModelConfig;
    isOpen: boolean;
    isToggling: boolean;
    isCopying: boolean;
    onOpenChange: (open: boolean) => void;
    onEdit: () => void;
    onToggleEnabled: () => void;
    onCopy: () => void;
    onDelete: () => void;
}

/**
 * Per-row dropdown menu — Edit, Enable/Disable, Copy, Delete.
 *
 * Mirrors the menu style used in `LLModelListItem` so the action
 * surface feels familiar across both the Providers drill-down and
 * the Configuration tab.
 *
 * The menu is rendered through a portal onto `document.body` and
 * positioned `fixed`, because the table wrapper carries
 * `overflow-hidden` (it clips the rows to the container's rounded
 * corners). An absolutely-positioned menu is clipped by that
 * ancestor — invisibly so on a long list, where the container is
 * tall enough to contain the menu anyway, but fatally on a provider
 * with only one or two models, where the menu has nowhere to render.
 * `position: fixed` escapes the clip; the portal keeps it out of the
 * table's stacking context as well.
 *
 * Position is measured on open and flips upward when the row sits
 * near the bottom of the viewport.
 */
const RowActionsMenu: React.FC<RowActionsMenuProps> = ({
    model,
    isOpen,
    isToggling,
    isCopying,
    onOpenChange,
    onEdit,
    onToggleEnabled,
    onCopy,
    onDelete,
}) => {
    const buttonRef = useRef<HTMLButtonElement>(null);
    const [menuPos, setMenuPos] = useState<{ top: number; left: number }>({ top: 0, left: 0 });

    // 220px ~ menu height with all 4 entries plus a divider; 192px = w-48.
    const MENU_HEIGHT = 220;
    const MENU_WIDTH = 192;

    const updateMenuPosition = useCallback(() => {
        if (!buttonRef.current) return;
        const rect = buttonRef.current.getBoundingClientRect();
        const spaceBelow = window.innerHeight - rect.bottom;
        const flipUp = spaceBelow < MENU_HEIGHT && rect.top > spaceBelow;
        setMenuPos({
            top: flipUp ? rect.top - MENU_HEIGHT - 4 : rect.bottom + 4,
            left: rect.right - MENU_WIDTH,
        });
    }, []);

    // A fixed menu does not travel with the row, so close it rather than
    // let it hang over unrelated content when the page moves underneath.
    useEffect(() => {
        if (!isOpen) return;
        updateMenuPosition();
        const close = () => onOpenChange(false);
        window.addEventListener('scroll', close, true);
        window.addEventListener('resize', close);
        return () => {
            window.removeEventListener('scroll', close, true);
            window.removeEventListener('resize', close);
        };
    }, [isOpen, updateMenuPosition, onOpenChange]);

    const rowBusy = isToggling || isCopying;

    return (
        <div className="relative inline-block">
            <button
                ref={buttonRef}
                onClick={() => {
                    if (!isOpen) updateMenuPosition();
                    onOpenChange(!isOpen);
                }}
                disabled={rowBusy}
                className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors disabled:opacity-50"
                title="Actions"
                aria-haspopup="menu"
                aria-expanded={isOpen}
            >
                {rowBusy ? (
                    <RefreshCw className="w-5 h-5 animate-spin" />
                ) : (
                    <MoreVertical className="w-5 h-5" />
                )}
            </button>

            {isOpen && createPortal(
                <>
                    <div
                        className="fixed inset-0"
                        style={{ zIndex: 9998 }}
                        onClick={() => onOpenChange(false)}
                    />
                    <div
                        role="menu"
                        data-testid="model-row-actions-menu"
                        className="w-48 bg-surface rounded-lg shadow-lg border border-default py-1"
                        style={{
                            position: 'fixed',
                            top: menuPos.top,
                            left: menuPos.left,
                            zIndex: 9999,
                        }}
                    >
                        <button
                            role="menuitem"
                            onClick={() => {
                                onEdit();
                                onOpenChange(false);
                            }}
                            className="w-full px-4 py-2 text-left text-sm hover:bg-surface-alt flex items-center space-x-2"
                        >
                            <Edit className="w-4 h-4 text-muted" />
                            <span>Edit</span>
                        </button>

                        <button
                            role="menuitem"
                            onClick={() => {
                                onToggleEnabled();
                                onOpenChange(false);
                            }}
                            disabled={isToggling}
                            className="w-full px-4 py-2 text-left text-sm hover:bg-surface-alt flex items-center space-x-2 disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                            <Power className={`w-4 h-4 ${model.enabled ? 'text-yellow-600' : 'text-green-600'}`} />
                            <span>{model.enabled ? 'Disable' : 'Enable'}</span>
                        </button>

                        <button
                            role="menuitem"
                            onClick={() => {
                                onCopy();
                                onOpenChange(false);
                            }}
                            disabled={isCopying}
                            className="w-full px-4 py-2 text-left text-sm hover:bg-surface-alt flex items-center space-x-2 disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                            <Copy className="w-4 h-4 text-muted" />
                            <span>Copy</span>
                        </button>

                        <div className="border-t border-default my-1" />

                        <button
                            role="menuitem"
                            onClick={() => {
                                onDelete();
                                onOpenChange(false);
                            }}
                            className="w-full px-4 py-2 text-left text-sm hover:bg-red-50 hover:text-red-600 flex items-center space-x-2 text-red-600"
                        >
                            <Trash2 className="w-4 h-4" />
                            <span>Delete</span>
                        </button>
                    </div>
                </>,
                document.body
            )}
        </div>
    );
};

const ProviderModelsView: React.FC<Props> = ({
    provider,
    onBack,
    onEditProvider,
    onOpenModel,
}) => {
    const { gatewayClient } = useGateway();
    const [models, setModels] = useState<LLModelConfig[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    // Filter / sort state — kept in component memory so the drill-down
    // resets to "show everything" each time it is opened. The list is
    // small enough that persisting these to localStorage would just be
    // surprising state for the next operator that opens the page.
    const [search, setSearch] = useState('');
    const [modeFilter, setModeFilter] = useState<string>('all');
    const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
    const [sortBy, setSortBy] = useState<SortKey>('name');
    const [sortDir, setSortDir] = useState<SortDir>('asc');

    // Action state — every action reuses the same backend endpoints
    // as the Configuration tab so behaviour stays consistent.
    const [editingModel, setEditingModel] = useState<LLModelConfig | null>(null);
    const [togglingId, setTogglingId] = useState<string | null>(null);
    const [copyingId, setCopyingId] = useState<string | null>(null);
    const [deleteTarget, setDeleteTarget] = useState<LLModelConfig | null>(null);
    const [deleting, setDeleting] = useState(false);
    const [deleteError, setDeleteError] = useState<string | null>(null);
    // The currently open row-action menu, if any. Single-state means
    // opening one menu auto-closes any other — operators can't end up
    // with two menus stacked on top of each other.
    const [openMenuId, setOpenMenuId] = useState<string | null>(null);

    const load = async () => {
        setLoading(true);
        setError(null);
        try {
            const resp = await gatewayClient.listLLMs();
            const all = Object.values(resp.models ?? {});
            // The models of this provider: the Control Plane stores a
            // model's provider as the provider's own lowercase key.
            setModels(all.filter(m => m.provider === provider.key));
        } catch (err) {
            const message = errorMessage(err) ?? String(err);
            setError(message);
            toast.error(`Failed to load models: ${message}`);
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [provider.id]);

    // Distinct modes present on this provider — drives the filter
    // dropdown. Sorting alphabetically by label keeps the list stable
    // across reloads even if the API order changes.
    const modeOptions = useMemo(() => {
        const distinct = new Set<string>();
        models.forEach(m => {
            if (m.mode) distinct.add(m.mode);
        });
        return Array.from(distinct).sort((a, b) =>
            modeLabelFor(a).localeCompare(modeLabelFor(b)),
        );
    }, [models]);

    const filteredAndSorted = useMemo(() => {
        const q = search.trim().toLowerCase();
        let list = models.slice();

        if (q) {
            list = list.filter(m =>
                (m.name ?? '').toLowerCase().includes(q)
                || (m.model_name ?? '').toLowerCase().includes(q)
                || (m.description ?? '').toLowerCase().includes(q),
            );
        }
        if (modeFilter !== 'all') {
            list = list.filter(m => m.mode === modeFilter);
        }
        if (statusFilter === 'enabled') {
            list = list.filter(m => m.enabled);
        } else if (statusFilter === 'disabled') {
            list = list.filter(m => !m.enabled);
        }

        const sortValue = (m: LLModelConfig): string | number => {
            switch (sortBy) {
                case 'name':
                    return (m.name ?? '').toLowerCase();
                case 'model':
                    return (m.model_name ?? '').toLowerCase();
                case 'mode':
                    return modeLabelFor(m.mode).toLowerCase();
                case 'status':
                    // Enabled sorts first ascending, disabled first
                    // descending — matches the visual expectation that
                    // "ascending status" surfaces the active models.
                    return m.enabled ? 0 : 1;
                case 'health':
                    return (m.health_status?.status ?? 'unknown').toLowerCase();
            }
        };

        list.sort((a, b) => {
            const av = sortValue(a);
            const bv = sortValue(b);
            let cmp: number;
            if (typeof av === 'number' && typeof bv === 'number') {
                cmp = av - bv;
            } else {
                cmp = String(av).localeCompare(String(bv));
            }
            // Stable secondary sort by name so equal keys come back in a
            // predictable order across renders.
            if (cmp === 0) {
                cmp = (a.name ?? '').localeCompare(b.name ?? '');
            }
            return sortDir === 'asc' ? cmp : -cmp;
        });

        return list;
    }, [models, search, modeFilter, statusFilter, sortBy, sortDir]);

    const enabledCount = models.filter(m => m.enabled).length;

    const handleToggleEnabled = async (model: LLModelConfig) => {
        const next = !model.enabled;
        setTogglingId(model.id);
        try {
            await gatewayClient.updateLLM(model.id, { enabled: next });
            toast.success(
                next
                    ? `Enabled "${model.name}"`
                    : `Disabled "${model.name}"`,
            );
            await load();
        } catch (err) {
            toast.error(
                `Failed to ${next ? 'enable' : 'disable'} "${model.name}": ${errorMessage(err) ?? err}`,
            );
        } finally {
            setTogglingId(null);
        }
    };

    const handleCopy = async (model: LLModelConfig) => {
        setCopyingId(model.id);
        try {
            // Server-side copy preserves the encrypted api_key, group
            // bindings, and every column on the source row. Cloning
            // client-side would silently drop the key (the API never
            // returns plaintext).
            const result = await gatewayClient.copyLLM(model.id);
            toast.success(`Model copied as "${result.model.name}"`);
            await load();
        } catch (err: any) {
            let message = err?.message ?? String(err);
            const detail = err?.response?.data?.detail;
            if (Array.isArray(detail)) {
                message = detail
                    .map((e: any) => `${e.loc?.join('.') ?? 'field'}: ${e.msg}`)
                    .join(', ');
            } else if (typeof detail === 'string') {
                message = detail;
            }
            toast.error(`Failed to copy model: ${message}`);
        } finally {
            setCopyingId(null);
        }
    };

    const confirmDelete = async () => {
        if (!deleteTarget) return;
        setDeleting(true);
        setDeleteError(null);
        try {
            await gatewayClient.deleteLLM(deleteTarget.id);
            toast.success(`Deleted model "${deleteTarget.name}"`);
            setDeleteTarget(null);
            await load();
        } catch (err) {
            setDeleteError(errorMessage(err) ?? 'Failed to delete model');
        } finally {
            setDeleting(false);
        }
    };

    const handleSaveEdit = async (updates: LLModelFormData) => {
        if (!editingModel) return;
        const modelId = editingModel.id;
        const modelName = updates.name || editingModel.name || 'Model';
        try {
            // The three fields of a catalog entry are not a model's.
            const { recommended, deprecated, icon_url, ...body } = updates;
            await gatewayClient.updateLLM(modelId, body);
            toast.success(`LLM Model "${modelName}" updated successfully`);
            setEditingModel(null);
            await load();
        } catch (err: any) {
            // Re-throw so LLModelForm can render its inline validation
            // panel — it expects the same shape (Error with optional
            // .validationErrors / multiline .message) that LLModelTab
            // produces.
            const serverError = new Error('Failed to update model') as any;
            const detail = err?.response?.data?.detail;
            if (Array.isArray(detail)) {
                serverError.validationErrors = detail;
                serverError.message = detail
                    .map((e: any) => `${e.loc?.slice(1).join('.') || 'field'}: ${e.msg}`)
                    .join('\n');
            } else if (typeof detail === 'string') {
                serverError.message = detail;
            } else {
                serverError.message = err?.message ?? 'Failed to update model';
            }
            throw serverError;
        }
    };

    const SortHeader: React.FC<{ label: string; columnKey: SortKey; align?: 'left' | 'right' }> = ({
        label,
        columnKey,
        align = 'left',
    }) => {
        const active = sortBy === columnKey;
        return (
            <button
                type="button"
                onClick={() => {
                    if (active) {
                        setSortDir(sortDir === 'asc' ? 'desc' : 'asc');
                    } else {
                        setSortBy(columnKey);
                        setSortDir('asc');
                    }
                }}
                className={`flex items-center gap-1 font-medium transition-colors ${
                    active ? 'text-default' : 'text-muted hover:text-default'
                } ${align === 'right' ? 'ml-auto' : ''}`}
                title={`Sort by ${label}`}
            >
                <span>{label}</span>
                <span className="text-xs">
                    {active ? (sortDir === 'asc' ? '↑' : '↓') : '↕'}
                </span>
            </button>
        );
    };

    return (
        <div className="app-column h-full">
            {/* Header */}
            <div className="flex-shrink-0 px-6 py-4 bg-surface border-b border-default">
                <div className="flex items-center gap-3 mb-3">
                    <button
                        onClick={onBack}
                        className="flex items-center gap-1.5 px-2.5 py-1.5 text-sm font-medium text-secondary hover:text-default hover:bg-surface-alt rounded-lg transition-colors"
                    >
                        <ArrowLeft className="w-4 h-4" />
                        All providers
                    </button>
                    <div className="flex-1 min-h-0 min-w-0" />
                    <button
                        onClick={() => void load()}
                        disabled={loading}
                        className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50"
                        title="Refresh"
                    >
                        <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
                        Refresh
                    </button>
                    <button
                        onClick={() => onEditProvider(provider)}
                        className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors"
                    >
                        <Edit className="w-4 h-4" />
                        Edit provider
                    </button>
                </div>
                <div className="flex items-center gap-4">
                    <ProviderIcon provider={provider} api={gatewayClient} size={48} />
                    <div className="flex-1 min-h-0 min-w-0">
                        <div className="flex items-center gap-2">
                            <h2 className="text-xl font-semibold text-default">
                                {provider.display_name}
                            </h2>
                            <code className="text-xs px-1.5 py-0.5 bg-surface-alt border border-default rounded">
                                {provider.key}
                            </code>
                            {!provider.enabled && (
                                <span className="inline-flex items-center px-2 py-0.5 text-xs font-medium bg-gray-50 text-gray-600 border border-gray-200 rounded">
                                    Disabled
                                </span>
                            )}
                        </div>
                        <div className="flex items-center gap-4 mt-1 text-sm text-secondary">
                            <span>
                                {models.length} model{models.length === 1 ? '' : 's'}
                                {' · '}
                                {enabledCount} enabled
                            </span>
                            {provider.developer_url && (
                                <a
                                    href={provider.developer_url}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="inline-flex items-center gap-1 text-primary-600 hover:underline"
                                >
                                    Developer site
                                    <ExternalLink className="w-3 h-3" />
                                </a>
                            )}
                            {provider.api_key_url && (
                                <a
                                    href={provider.api_key_url}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="inline-flex items-center gap-1 text-primary-600 hover:underline"
                                >
                                    API keys
                                    <ExternalLink className="w-3 h-3" />
                                </a>
                            )}
                        </div>
                    </div>
                </div>
            </div>

            {/* Filter / sort toolbar */}
            <div className="flex-shrink-0 px-6 py-3 bg-surface-alt border-b border-default flex flex-wrap items-center gap-3">
                <input
                    type="text"
                    value={search}
                    onChange={e => setSearch(e.target.value)}
                    placeholder="Search models…"
                    className="flex-1 min-w-[200px] max-w-sm px-3 py-1.5 text-sm bg-surface border border-default rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent min-h-0 min-w-0"
                />

                <label className="flex items-center gap-2 text-sm text-secondary">
                    <span>Mode</span>
                    <select
                        value={modeFilter}
                        onChange={e => setModeFilter(e.target.value)}
                        className="px-2 py-1.5 text-sm bg-surface border border-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
                    >
                        <option value="all">
                            All ({models.length})
                        </option>
                        {modeOptions.map(mode => {
                            const count = models.filter(m => m.mode === mode).length;
                            return (
                                <option key={mode} value={mode}>
                                    {modeLabelFor(mode)} ({count})
                                </option>
                            );
                        })}
                    </select>
                </label>

                <label className="flex items-center gap-2 text-sm text-secondary">
                    <span>Status</span>
                    <select
                        value={statusFilter}
                        onChange={e => setStatusFilter(e.target.value as StatusFilter)}
                        className="px-2 py-1.5 text-sm bg-surface border border-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
                    >
                        <option value="all">All</option>
                        <option value="enabled">Enabled</option>
                        <option value="disabled">Disabled</option>
                    </select>
                </label>

                <label className="flex items-center gap-2 text-sm text-secondary">
                    <span>Sort by</span>
                    <select
                        value={sortBy}
                        onChange={e => setSortBy(e.target.value as SortKey)}
                        className="px-2 py-1.5 text-sm bg-surface border border-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
                    >
                        <option value="name">Name</option>
                        <option value="model">Model</option>
                        <option value="mode">Mode</option>
                        <option value="status">Status</option>
                        <option value="health">Health</option>
                    </select>
                    <button
                        type="button"
                        onClick={() => setSortDir(sortDir === 'asc' ? 'desc' : 'asc')}
                        className="px-2 py-1.5 text-sm bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors"
                        title={sortDir === 'asc' ? 'Ascending — click to reverse' : 'Descending — click to reverse'}
                        aria-label={`Toggle sort direction (currently ${sortDir})`}
                    >
                        {sortDir === 'asc' ? '↑' : '↓'}
                    </button>
                </label>

                <span className="ml-auto text-xs text-muted">
                    Showing {filteredAndSorted.length} of {models.length}
                </span>
            </div>

            {/* Body */}
            <div className="flex-1 overflow-auto px-6 py-4 min-h-0 min-w-0">
                {loading ? (
                    <div className="flex items-center justify-center h-32 text-muted">
                        <RefreshCw className="w-5 h-5 animate-spin mr-2" />
                        Loading models…
                    </div>
                ) : error ? (
                    <div className="bg-red-50 border border-red-200 rounded-lg p-3 flex items-start gap-2">
                        <AlertCircle className="w-4 h-4 text-red-600 flex-shrink-0 mt-0.5" />
                        <pre className="text-sm text-red-700 whitespace-pre-wrap font-mono flex-1 min-h-0 min-w-0">
                            {error}
                        </pre>
                    </div>
                ) : models.length === 0 ? (
                    <div className="flex flex-col items-center justify-center h-48 text-muted">
                        <AlertCircle className="w-8 h-8 mb-2" />
                        <p className="text-sm">
                            No LLM models reference this provider yet.
                        </p>
                        <p className="text-xs mt-1">
                            Add one from <span className="font-medium">AI Models → Configuration</span>.
                        </p>
                    </div>
                ) : filteredAndSorted.length === 0 ? (
                    <div className="flex flex-col items-center justify-center h-48 text-muted">
                        <AlertCircle className="w-8 h-8 mb-2" />
                        <p className="text-sm">
                            No models match the current filters.
                        </p>
                        <button
                            onClick={() => {
                                setSearch('');
                                setModeFilter('all');
                                setStatusFilter('all');
                            }}
                            className="text-xs mt-1 text-primary-600 hover:underline"
                        >
                            Clear filters
                        </button>
                    </div>
                ) : (
                    <div className="bg-surface border border-default rounded-lg overflow-hidden">
                        <table className="w-full text-sm">
                            <thead className={TABLE_THEAD}>
                                <tr className="text-left">
                                    <th className={`px-4 py-2 ${TABLE_TH_TYPE}`}>
                                        <SortHeader label="Name" columnKey="name" />
                                    </th>
                                    <th className={`px-4 py-2 ${TABLE_TH_TYPE}`}>
                                        <SortHeader label="Model" columnKey="model" />
                                    </th>
                                    <th className={`px-4 py-2 ${TABLE_TH_TYPE}`}>
                                        <SortHeader label="Mode" columnKey="mode" />
                                    </th>
                                    <th className={`px-4 py-2 ${TABLE_TH_TYPE}`}>Cost</th>
                                    <th className={`px-4 py-2 ${TABLE_TH_TYPE}`}>
                                        <SortHeader label="Status" columnKey="status" />
                                    </th>
                                    <th className={`px-4 py-2 ${TABLE_TH_TYPE}`}>
                                        <SortHeader label="Health" columnKey="health" />
                                    </th>
                                    <th className={`px-4 py-2 text-right ${TABLE_TH_TYPE}`}>Actions</th>
                                </tr>
                            </thead>
                            <tbody className={TABLE_TBODY}>
                                {filteredAndSorted.map(model => {
                                    const healthKey = model.health_status?.status ?? 'unknown';
                                    const health = HEALTH_BADGE[healthKey] ?? HEALTH_BADGE.unknown;
                                    const isToggling = togglingId === model.id;
                                    const isCopying = copyingId === model.id;
                                    return (
                                        <tr
                                            key={model.id}
                                            className="hover:bg-surface-alt transition-colors"
                                            data-llm-model-id={model.id}
                                        >
                                            <td className="px-4 py-2.5 font-medium text-default">
                                                {model.name}
                                                {model.description && (
                                                    <div className="text-xs text-muted mt-0.5 line-clamp-1">
                                                        {model.description}
                                                    </div>
                                                )}
                                            </td>
                                            <td className="px-4 py-2.5">
                                                <code className="text-xs px-1.5 py-0.5 bg-surface-alt border border-default rounded">
                                                    {model.model_name}
                                                </code>
                                            </td>
                                            <td className="px-4 py-2.5 text-secondary">
                                                {modeLabelFor(model.mode)}
                                            </td>
                                            <td className="px-4 py-2.5 text-secondary whitespace-nowrap">
                                                {formatCostByMode(model) ?? (
                                                    <span
                                                        className="text-muted"
                                                        title="No pricing on file. Edit the model to add it."
                                                    >
                                                        —
                                                    </span>
                                                )}
                                            </td>
                                            <td className="px-4 py-2.5">
                                                {model.enabled ? (
                                                    <span className="inline-flex items-center px-2 py-0.5 text-xs font-medium bg-green-50 text-green-700 border border-green-200 rounded">
                                                        Enabled
                                                    </span>
                                                ) : (
                                                    <span className="inline-flex items-center px-2 py-0.5 text-xs font-medium bg-gray-50 text-gray-600 border border-gray-200 rounded">
                                                        Disabled
                                                    </span>
                                                )}
                                            </td>
                                            <td className="px-4 py-2.5">
                                                <span
                                                    className={`inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium border rounded ${health.className}`}
                                                >
                                                    <Activity className="w-3 h-3" />
                                                    {health.label}
                                                </span>
                                            </td>
                                            <td className="px-4 py-2.5">
                                                <div className="flex justify-end">
                                                    <RowActionsMenu
                                                        model={model}
                                                        isOpen={openMenuId === model.id}
                                                        isToggling={isToggling}
                                                        isCopying={isCopying}
                                                        onOpenChange={open =>
                                                            setOpenMenuId(open ? model.id : null)
                                                        }
                                                        onEdit={() => {
                                                            // Prefer the local edit modal so the
                                                            // operator stays in the Providers
                                                            // context. The optional callback is
                                                            // honoured for a caller that wants
                                                            // to deep-link into the
                                                            // Configuration tab.
                                                            if (onOpenModel) {
                                                                onOpenModel(model.id);
                                                            } else {
                                                                setEditingModel(model);
                                                            }
                                                        }}
                                                        onToggleEnabled={() =>
                                                            void handleToggleEnabled(model)
                                                        }
                                                        onCopy={() => void handleCopy(model)}
                                                        onDelete={() => {
                                                            setDeleteError(null);
                                                            setDeleteTarget(model);
                                                        }}
                                                    />
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

            {/* Edit Model Modal — same form used on the Configuration tab. */}
            {editingModel && (
                <LLModelForm
                    model={editingModel}
                    onClose={() => setEditingModel(null)}
                    onSave={handleSaveEdit}
                />
            )}

            {/* Delete confirmation — shared modal so the experience matches
                Resources → AI Models → Configuration. */}
            <GenericDeleteConfirmationModal
                isOpen={deleteTarget !== null}
                onClose={() => {
                    setDeleteTarget(null);
                    setDeleteError(null);
                }}
                onConfirm={asyncHandler(confirmDelete, 'confirm')}
                itemType="model"
                itemName={deleteTarget?.name ?? ''}
                itemInfo={deleteTarget ? {
                    id: deleteTarget.id,
                    provider: deleteTarget.provider,
                    model_name: deleteTarget.model_name,
                    description: deleteTarget.description,
                    enabled: deleteTarget.enabled,
                } : undefined}
                isDeleting={deleting}
                errorMessage={deleteError}
            />
        </div>
    );
};

export default ProviderModelsView;

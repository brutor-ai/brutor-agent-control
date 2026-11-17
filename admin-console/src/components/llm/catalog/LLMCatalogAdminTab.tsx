/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * System-Admin → LLM Catalog page.
 *
 * The global LLM catalog is the single, platform-wide source of truth for
 * "what models can be browsed and imported" by tenants. Tenant admins
 * browse + import entries (read-only to them); system admins CURATE the
 * catalog here — create custom entries, edit descriptive metadata, and
 * delete entries.
 *
 * Sysadmin-only (the nav item carries `requiresSystemAdmin`). The catalog
 * is GLOBAL (no tenant scope) so no tenant_id is passed. Data fetching is
 * hand-rolled (useState/useEffect/useCallback) — NO react-query — mirroring
 * the AI Assets page conventions.
 *
 * The create/edit form is the shared `LLModelForm` rendered with
 * `variant="catalog"` (the same component the per-tenant LLM Models page
 * uses for instances), with instance-only tabs hidden and a few
 * catalog-only fields surfaced. The values shown are the IMPORT DEFAULTS
 * materialised onto a configured llm_model when a tenant imports the entry.
 * Two small adapters bridge the catalog shape (`LLMCatalogEntry`) and the
 * form's instance-shaped `LLModelConfig` props/output.
 */

import React, { useCallback, useEffect, useState } from 'react';
import { TABLE_TH_TYPE } from '../../ui/tableStyles';
import { Library, Loader2, Pencil, Plus, Search, Trash2 } from 'lucide-react';

import { useGateway } from '../../../hooks/useGateway';
import { useDebouncedValue } from '../../../hooks/useDebouncedValue';
import { useToast } from '../../common/Toast';
import { useConfirm } from '../../common/ConfirmationDialog';
import InfoBanner from '../../common/InfoBanner';
import { ProviderLogo } from '../config/providerLogos';
import LLModelForm, { type LLModelFormData } from '../config/LLModelForm';
import type {
    LLMCatalogEntry,
    LLMCatalogCreateRequest,
    LLModelConfig,
} from '../../../lib/llmTypes';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import { getNumber } from '../../../lib/narrow';

const PAGE_LIMIT = 100;

// Mirrors LLModelMode / CATALOG_MODES on the backend.
const CATALOG_MODES = [
    'chat',
    'responses',
    'completion',
    'embedding',
    'image_generation',
    'audio_transcription',
    'audio_speech',
    'video_generation',
    'rerank',
    'moderation',
    'routing',
] as const;

const TH =
    `px-6 py-3 text-left ${TABLE_TH_TYPE}`;

const filterSelect =
    'px-3 py-2 rounded-lg border border-strong bg-surface text-sm text-default focus:outline-none focus:ring-2 focus:ring-primary-500';

// ---------------------------------------------------------------------------
// Catalog ↔ form adapters
// ---------------------------------------------------------------------------
//
// `LLModelForm` is instance-shaped (`LLModelConfig` in, `LLModelFormData`
// out). The catalog has its own column names (display_name vs name,
// provider_key vs provider, default_cost_per_input_token vs
// cost_per_input_token, max_output_tokens vs default_max_tokens,
// context_window as a top-level column vs inside `capabilities`). These two
// functions translate in both directions.

/** Catalog entry → the `model` prop LLModelForm expects (or null for create). */
function entryToFormModel(entry: LLMCatalogEntry | null): LLModelConfig | null {
    if (!entry) return null;

    // The form reads context_window off capabilities.context_window. The
    // catalog stores it as a dedicated column, so merge it in (entry-level
    // column wins) while preserving any other capability keys.
    const capabilities: Record<string, unknown> = {
        ...(entry.capabilities ?? {}),
    };
    if (entry.context_window != null) {
        capabilities.context_window = entry.context_window;
    }

    // The form reads supports_* off the top-level model row. Catalog entries
    // may carry them inside `capabilities`; surface them if present so the
    // capability checkboxes round-trip.
    const caps = entry.capabilities ?? {};
    const boolCap = (k: string): boolean | undefined =>
        typeof caps[k] === 'boolean' ? (caps[k]) : undefined;

    return {
        // Identity / descriptive
        id: entry.id,
        name: entry.display_name,
        description: entry.description ?? undefined,
        provider: entry.provider_key,
        model_name: entry.model_name,
        mode: entry.mode,
        tags: entry.tags,
        parameters: entry.parameters,
        // Cost (catalog default names → instance names)
        cost_per_input_token: entry.default_cost_per_input_token ?? undefined,
        cost_per_output_token: entry.default_cost_per_output_token ?? undefined,
        default_max_tokens: entry.max_output_tokens ?? undefined,
        capabilities,
        // Capability flags (best-effort from the entry's capabilities blob)
        supports_streaming: boolCap('supports_streaming'),
        supports_function_calling: boolCap('supports_function_calling'),
        supports_vision: boolCap('supports_vision'),
        supports_batch: boolCap('supports_batch'),
        thinking_enabled: entry.thinking_enabled,
        retire_date: entry.retire_date,
        additional_drop_params: entry.additional_drop_params,
        // Media (passed straight through)
        cost_per_image: entry.cost_per_image,
        supported_image_sizes: entry.supported_image_sizes,
        supported_image_qualities: entry.supported_image_qualities,
        max_images_per_request: entry.max_images_per_request,
        cost_per_audio_char: entry.cost_per_audio_char,
        supported_voices: entry.supported_voices,
        supported_audio_formats: entry.supported_audio_formats,
        cost_per_audio_minute: entry.cost_per_audio_minute,
        max_audio_duration_seconds: entry.max_audio_duration_seconds,
        supported_input_formats: entry.supported_input_formats,
        supported_response_formats: entry.supported_response_formats,
        cost_per_video_second: entry.cost_per_video_second,
        supported_video_resolutions: entry.supported_video_resolutions,
        supported_video_durations: entry.supported_video_durations,
        max_video_duration_seconds: entry.max_video_duration_seconds,
        // Catalog-only fields surfaced on the General tab.
        recommended: entry.recommended,
        deprecated: entry.deprecated,
        icon_url: entry.icon_url,
        // Fields LLModelConfig declares as required but the catalog form
        // never reads — provide inert defaults so the synthesized object
        // satisfies the type without leaking instance semantics.
        tenant_id: '',
        api_url_auto_discovered: true,
        auth_type: 'api_key',
        default_temperature: 0.7,
        routing_weight: 1,
        timeout: 60,
        enabled: true,
        health_status: { status: 'unknown', consecutive_failures: 0 },
        performance_metrics: {
            total_requests: 0,
            failed_requests: 0,
            success_rate: 0,
            total_tokens_consumed: 0,
        },
        created_at: entry.created_at ?? '',
        updated_at: entry.updated_at ?? '',
    } as unknown as LLModelConfig & {
        recommended?: boolean;
        deprecated?: boolean;
        icon_url?: string | null;
    };
}

// ---------------------------------------------------------------------------
// Main tab
// ---------------------------------------------------------------------------

const LLMCatalogAdminTab: React.FC = () => {
    const { gatewayClient } = useGateway();
    const toast = useToast();
    const confirm = useConfirm();

    const [entries, setEntries] = useState<LLMCatalogEntry[]>([]);
    const [total, setTotal] = useState(0);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    // Filters
    const [providerKey, setProviderKey] = useState('');
    const [mode, setMode] = useState('');
    const [q, setQ] = useState('');
    // The input keeps the raw value; only the settled one reaches `refresh`.
    // Undebounced, every keystroke would be a listLLMCatalog round trip.
    const debouncedQ = useDebouncedValue(q, 300);
    const [offset, setOffset] = useState(0);

    // Form (create / edit). `creating` opens an empty catalog form; `editing`
    // holds the entry being edited.
    const [creating, setCreating] = useState(false);
    const [editing, setEditing] = useState<LLMCatalogEntry | null>(null);

    const refresh = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const res = await gatewayClient.listLLMCatalog({
                provider_key: providerKey.trim() || undefined,
                mode: mode || undefined,
                q: debouncedQ.trim() || undefined,
                limit: PAGE_LIMIT,
                offset,
            });
            setEntries(res.catalog);
            setTotal(res.total);
        } catch (e: unknown) {
            const msg = e instanceof Error ? e.message : String(e);
            setError(msg);
        } finally {
            setLoading(false);
        }
    }, [gatewayClient, providerKey, mode, debouncedQ, offset]);

    useEffect(() => {
        runBackground(refresh(), 'refresh');
    }, [refresh]);

    // form `onSave(modelData)` → catalog create/update body. `original` is
    // the entry being edited (null on create) and carries the silent fields
    // we round-trip but don't surface as inputs.
    const handleSave = useCallback(
        async (
            modelData: LLModelFormData,
            original: LLMCatalogEntry | null,
        ) => {
            const contextWindow = getNumber(modelData.capabilities, 'context_window') ?? null;

            // Catalog-default capability/media metadata shared by create +
            // update. Media fields flow straight from the form; for non-media
            // modes the form omits them, so fall back to the original entry's
            // values to avoid wiping curated data on an edit.
            const capabilityFields = {
                cost_per_image:
                    modelData.cost_per_image ?? original?.cost_per_image,
                cost_per_audio_char:
                    modelData.cost_per_audio_char ??
                    original?.cost_per_audio_char,
                cost_per_audio_minute:
                    modelData.cost_per_audio_minute ??
                    original?.cost_per_audio_minute,
                cost_per_video_second:
                    modelData.cost_per_video_second ??
                    original?.cost_per_video_second,
                supported_voices:
                    modelData.supported_voices ?? original?.supported_voices,
                supported_image_sizes:
                    modelData.supported_image_sizes ??
                    original?.supported_image_sizes,
                supported_image_qualities:
                    modelData.supported_image_qualities ??
                    original?.supported_image_qualities,
                supported_audio_formats:
                    modelData.supported_audio_formats ??
                    original?.supported_audio_formats,
                supported_video_resolutions:
                    modelData.supported_video_resolutions ??
                    original?.supported_video_resolutions,
                supported_video_durations:
                    modelData.supported_video_durations ??
                    original?.supported_video_durations,
                max_images_per_request:
                    modelData.max_images_per_request ??
                    original?.max_images_per_request,
                max_audio_duration_seconds:
                    modelData.max_audio_duration_seconds ??
                    original?.max_audio_duration_seconds,
                max_video_duration_seconds:
                    modelData.max_video_duration_seconds ??
                    original?.max_video_duration_seconds,
                thinking_enabled: modelData.thinking_enabled ?? false,
                parameters: modelData.parameters,
                tags: modelData.tags,
                retire_date: modelData.retire_date || undefined,
                // Silent fields — surfaced nowhere in the catalog form, but
                // round-tripped so an edit doesn't drop them.
                additional_drop_params:
                    modelData.additional_drop_params ??
                    original?.additional_drop_params,
                supported_input_formats:
                    modelData.supported_input_formats ??
                    original?.supported_input_formats,
                supported_response_formats:
                    (modelData.supported_response_formats ??
                        original?.supported_response_formats) ||
                    undefined,
            };

            if (!original) {
                const body: LLMCatalogCreateRequest = {
                    provider_key: (modelData.provider || '').trim().toLowerCase(),
                    model_name: (modelData.model_name || '').trim(),
                    display_name: (modelData.name || '').trim(),
                    mode: modelData.mode || 'chat',
                    description: modelData.description?.trim() || null,
                    icon_url: modelData.icon_url || null,
                    context_window: contextWindow,
                    max_output_tokens: modelData.default_max_tokens ?? null,
                    default_cost_per_input_token:
                        modelData.cost_per_input_token ?? null,
                    default_cost_per_output_token:
                        modelData.cost_per_output_token ?? null,
                    recommended: modelData.recommended ?? false,
                    deprecated: modelData.deprecated ?? false,
                    ...capabilityFields,
                };
                await gatewayClient.createLLMCatalogEntry(body);
                toast.success('Catalog entry created');
            } else {
                // provider_key / model_name are immutable on update.
                await gatewayClient.updateLLMCatalogEntry(original.id, {
                    display_name: (modelData.name || '').trim(),
                    mode: modelData.mode || 'chat',
                    description: modelData.description?.trim() || null,
                    icon_url: modelData.icon_url || null,
                    context_window: contextWindow,
                    max_output_tokens: modelData.default_max_tokens ?? null,
                    default_cost_per_input_token:
                        modelData.cost_per_input_token ?? null,
                    default_cost_per_output_token:
                        modelData.cost_per_output_token ?? null,
                    recommended: modelData.recommended ?? false,
                    deprecated: modelData.deprecated ?? false,
                    ...capabilityFields,
                });
                toast.success('Catalog entry updated');
            }
            await refresh();
        },
        [gatewayClient, toast, refresh],
    );

    async function handleDelete(entry: LLMCatalogEntry) {
        const label = entry.display_name || entry.model_name;
        const ok = await confirm({
            title: 'Delete catalog entry',
            message: `Delete "${label}" from the global LLM catalog? Tenants will no longer be able to import it. Already-imported models are unaffected.`,
            confirmLabel: 'Delete',
            variant: 'danger',
        });
        if (!ok) return;
        try {
            await gatewayClient.deleteLLMCatalogEntry(entry.id);
            toast.success('Catalog entry deleted');
            await refresh();
        } catch (e: unknown) {
            const msg = e instanceof Error ? e.message : String(e);
            toast.error('Delete failed', msg);
        }
    }

    function resetFilters() {
        setProviderKey('');
        setMode('');
        setQ('');
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
                        <Library className="w-7 h-7 text-primary-600" />
                    </div>
                    <div>
                        <h1 className="text-2xl font-bold text-default">
                            LLM Catalog
                        </h1>
                        <p className="text-sm text-muted">
                            Curate the global catalog of models tenants can browse
                            and import.
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

            <InfoBanner title="What the global LLM catalog is">
                <p>
                    The catalog is the platform-wide source of truth for the models
                    every tenant can browse and import. Tenant admins import an
                    entry to create a configured model in their tenant. As a system
                    admin you curate the catalog here — add custom entries, edit
                    descriptive metadata, mark entries{' '}
                    <strong>recommended</strong> or <strong>deprecated</strong>, and
                    delete entries. Deleting an entry does not affect models a tenant
                    has already imported.
                </p>
            </InfoBanner>

            {/* Filters */}
            <div className="flex flex-wrap items-center gap-3">
                <input
                    type="text"
                    value={providerKey}
                    onChange={(e) => {
                        setProviderKey(e.target.value);
                        setOffset(0);
                    }}
                    placeholder="Provider key (e.g. openai)"
                    className={`${filterSelect} min-w-[200px]`}
                    aria-label="Filter by provider key"
                />

                <select
                    value={mode}
                    onChange={(e) => {
                        setMode(e.target.value);
                        setOffset(0);
                    }}
                    className={filterSelect}
                    aria-label="Filter by mode"
                >
                    <option value="">All modes</option>
                    {CATALOG_MODES.map((m) => (
                        <option key={m} value={m}>
                            {m}
                        </option>
                    ))}
                </select>

                <div className="relative flex-1 min-w-[200px] min-h-0 min-w-0">
                    <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted pointer-events-none" />
                    <input
                        type="text"
                        value={q}
                        onChange={(e) => {
                            // Reset the page with the query, not on blur/Enter.
                            // A new search issued at the old offset asks for
                            // rows 100-149 of a result set that may hold 12,
                            // and the table renders empty while the footer
                            // still claims page 3 — which reads as "the model
                            // is not in the catalog".
                            setQ(e.target.value);
                            setOffset(0);
                        }}
                        placeholder="Search display name / model name…"
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
                    <Library className="w-10 h-10 text-muted mx-auto mb-3" />
                    <h3 className="text-base font-semibold text-default mb-1">
                        No catalog entries
                    </h3>
                    <p className="text-sm text-muted max-w-md mx-auto">
                        Click <strong>Add entry</strong> to create a custom catalog
                        entry.
                    </p>
                </div>
            ) : (
                <div className="bg-surface border border-default rounded-lg shadow-sm overflow-hidden">
                    <div className="overflow-x-auto">
                        <table className="min-w-full divide-y divide-default">
                            <thead className="bg-surface-alt">
                                <tr>
                                    <th className={TH}>Asset</th>
                                    <th className={TH}>Provider</th>
                                    <th className={TH}>Model name</th>
                                    <th className={TH}>Mode</th>
                                    <th className={TH}>Flags</th>
                                    <th className={`${TH} text-right`}>Actions</th>
                                </tr>
                            </thead>
                            <tbody className="bg-surface divide-y divide-default">
                                {entries.map((e) => (
                                    <tr
                                        key={e.id}
                                        className="hover:bg-surface-alt transition-colors"
                                    >
                                        {/* Asset (icon + display name) */}
                                        <td className="px-6 py-4">
                                            <div className="flex items-center gap-3 min-w-0">
                                                {e.icon_url ? (
                                                    <img
                                                        src={e.icon_url}
                                                        alt={`${e.display_name} icon`}
                                                        className="w-5 h-5 object-contain flex-shrink-0"
                                                    />
                                                ) : (
                                                    <ProviderLogo
                                                        provider={e.provider_key}
                                                        size="md"
                                                        className="flex-shrink-0"
                                                    />
                                                )}
                                                <div className="min-w-0">
                                                    <div className="font-medium text-default truncate max-w-xs">
                                                        {e.display_name}
                                                    </div>
                                                    <div className="text-xs text-muted font-mono truncate max-w-xs">
                                                        {e.source}
                                                    </div>
                                                </div>
                                            </div>
                                        </td>
                                        {/* Provider */}
                                        <td className="px-6 py-4 whitespace-nowrap text-sm text-secondary">
                                            {e.provider_key}
                                        </td>
                                        {/* Model name */}
                                        <td className="px-6 py-4 whitespace-nowrap text-xs font-mono text-secondary">
                                            {e.model_name}
                                        </td>
                                        {/* Mode */}
                                        <td className="px-6 py-4 whitespace-nowrap">
                                            <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-surface-raised text-secondary border border-default">
                                                {e.mode}
                                            </span>
                                        </td>
                                        {/* Flags */}
                                        <td className="px-6 py-4 whitespace-nowrap">
                                            <div className="flex flex-wrap gap-1">
                                                {e.recommended && (
                                                    <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-green-50 text-green-700 border border-green-200">
                                                        Recommended
                                                    </span>
                                                )}
                                                {e.deprecated && (
                                                    <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-amber-50 text-amber-700 border border-amber-200">
                                                        Deprecated
                                                    </span>
                                                )}
                                                {!e.recommended && !e.deprecated && (
                                                    <span className="text-xs text-muted">
                                                        —
                                                    </span>
                                                )}
                                            </div>
                                        </td>
                                        {/* Actions */}
                                        <td className="px-6 py-4 whitespace-nowrap text-right">
                                            <div className="inline-flex items-center gap-1">
                                                <button
                                                    onClick={() => setEditing(e)}
                                                    className="p-2 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors"
                                                    title="Edit entry"
                                                    aria-label={`Edit ${e.display_name}`}
                                                >
                                                    <Pencil className="w-4 h-4" />
                                                </button>
                                                <button
                                                    onClick={asyncHandler(() => handleDelete(e), 'click')}
                                                    className="p-2 text-muted hover:text-red-600 hover:bg-red-50 rounded-lg transition-colors"
                                                    title="Delete entry"
                                                    aria-label={`Delete ${e.display_name}`}
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
                <LLModelForm
                    variant="catalog"
                    model={null}
                    onClose={() => setCreating(false)}
                    onSave={async (modelData) => {
                        await handleSave(modelData, null);
                    }}
                />
            )}

            {editing && (
                <LLModelForm
                    variant="catalog"
                    model={entryToFormModel(editing)}
                    onClose={() => setEditing(null)}
                    onSave={async (modelData) => {
                        await handleSave(modelData, editing);
                    }}
                />
            )}
        </div>
    );
};

export default LLMCatalogAdminTab;

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useRef, useCallback } from 'react';
import { TABLE_TH_TYPE } from '../../ui/tableStyles';
import toast from 'react-hot-toast';
import {
    RefreshCw, Activity, AlertCircle, Plus, Link,
    PauseCircle, LayoutGrid, List, FolderTree, Search, X,
    CheckCircle2, Key, Users, Edit, HelpCircle, Download, Power,
} from 'lucide-react';
import { useGateway } from '../../../hooks/useGateway';
import { useAIModelHealthCheck } from '../../../hooks/useAIModelHealthCheck';
import { useLocalStorageString } from '../../../hooks/useLocalStorage';
import { LLModelConfig, LLModelCreateRequest, HealthCheckResponse, LLMCatalogEntry, LLMCatalogImportRequest } from '../../../lib/llmTypes';
import LLMCatalogApiUrlDialog from './LLMCatalogApiUrlDialog';
import LLMCatalogTree from './LLMCatalogTree';
import { apiUrlRefusal, isBedrockProvider, isVertexProvider, modelSaveRefusalText } from './providerConnection';
import LLModelCard from './LLModelCard';
import LLModelListItem from './LLModelListItem';
import LLModelForm, { type LLModelFormData } from './LLModelForm';
import HealthCheckHelpModal from './HealthCheckHelpModal';
import LLMProxyEndpointModal from './LLMProxyEndpointModal';
import LLMSearchModal from './LLMSearchModal';
import GenericDeleteConfirmationModal from '../../common/GenericDeleteConfirmationModal';
import { failureSentence, errorKind, errorMessage } from '../../../lib/narrow';
import { modelErrorText } from '../../../lib/dispatchRefusal';
import { saveFailure } from '../../../lib/validationErrors';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import { useSlot } from '../../../app/slots';
import ReadFailureNotice from '../../common/ReadFailureNotice';

interface LLModelTabProps {
    /**
     * If provided, the tab boots with this model selected and
     * scrolled into view. Used by Mission Control's System-Alerts
     * deep-link: clicking an alert with `resource_type === 'llm_model'`
     * jumps straight to the offending model so the operator can
     * inspect its config / health without scrolling through the
     * full catalogue.
     */
    initialFocusModelId?: string | null;
}

const LLModelTab: React.FC<LLModelTabProps> = ({ initialFocusModelId = null }) => {
    const { gatewayClient, llmProxyUrl } = useGateway();

    // Component state
    const [models, setModels] = useState<Record<string, LLModelConfig>>({});
    const [loading, setLoading] = useState(true);
    const hasLoadedRef = useRef(false);
    const [testingConnections, setTestingConnections] = useState<Set<string>>(new Set());
    const [editingModel, setEditingModel] = useState<LLModelConfig | null>(null);
    const [isCreatingNew, setIsCreatingNew] = useState(false);
    const [newModelPrefill, setNewModelPrefill] = useState<Partial<LLModelConfig> | null>(null);
    const [selectedModelId, setSelectedModelId] = useState<string | null>(null);
    const [showHealthHelp, setShowHealthHelp] = useState(false);
    const [showProxyModal, setShowProxyModal] = useState(false);
    const [showBrowseModal, setShowBrowseModal] = useState(false);
    // Global LLM catalog (browse → import). Loaded lazily when the Browse
    // modal opens; the catalog is the 300+ available models, distinct from
    // the configured `models` instances above.
    const [catalogEntries, setCatalogEntries] = useState<LLMCatalogEntry[]>([]);
    const [catalogLoading, setCatalogLoading] = useState(false);
    const [catalogError, setCatalogError] = useState<string | null>(null);
    const [showSearchModal, setShowSearchModal] = useState(false);
    // The catalog entry whose API URL is being asked for before it is imported.
    const [apiUrlQuestion, setApiUrlQuestion] = useState<{
        entry: LLMCatalogEntry;
        refusal?: string;
        done: () => void;
    } | null>(null);
    // Import sources come from the `llm-models.import` slot; this is the id
    // of the one whose dialog is open.
    const importSources = useSlot('llm-models.import');
    const [openImport, setOpenImport] = useState<string | null>(null);
    const [timeDisplayKey, setTimeDisplayKey] = useState(0);

    // The source model being copied — non-null while the server-side
    // copy is in flight. Drives the overlay spinner so the admin has
    // unambiguous feedback during the network round-trip (encrypted
    // key cloning + group rebinding can take a few hundred ms).
    const [copyingModel, setCopyingModel] = useState<LLModelConfig | null>(null);

    // Delete confirmation modal state
    const [deleteConfirmation, setDeleteConfirmation] = useState<{
        modelId: string;
        modelName: string;
        modelInfo: LLModelConfig;
    } | null>(null);
    const [isDeleting, setIsDeleting] = useState(false);
    const [deleteError, setDeleteError] = useState<string | null>(null);

    // "Model enabled" notice modal state
    const [enabledNotice, setEnabledNotice] = useState<{
        modelId: string;
        modelName: string;
        /**
         * The cloud of a model that was added disabled: a model on Amazon
         * Bedrock or Vertex AI arrives switched off, because it has no
         * credential until the administrator enters one.
         */
        addedDisabledOn?: 'Amazon Bedrock' | 'Vertex AI';
    } | null>(null);
    const [togglingModel, setTogglingModel] = useState<{ name: string; enabling: boolean } | null>(null);


    // Persist layout preference using custom hook
    const [layout, setLayout] = useLocalStorageString('llm-models-view-layout', 'card') as ['card' | 'list', (value: 'card' | 'list') => void];

    // Persist the sort preference the same way — operators expect the
    // list to come back in whatever order they picked last session.
    // Default to alphabetical so a fresh tenant sees a predictable
    // ordering that matches how models are grouped in the catalog.
    type SortKey = 'name' | 'provider' | 'size' | 'requests' | 'tokens' | 'updated';
    type SortDir = 'asc' | 'desc';
    const [sortBy, setSortBy] = useLocalStorageString(
        'llm-models-sort-by', 'name',
    ) as [SortKey, (v: SortKey) => void];
    const [sortDir, setSortDir] = useLocalStorageString(
        'llm-models-sort-dir', 'asc',
    ) as [SortDir, (v: SortDir) => void];

    // Track if any form/modal is open (pause health checks during editing)
    const isAnyFormOpen = isCreatingNew || editingModel !== null || showHealthHelp || showProxyModal || showBrowseModal || showSearchModal || apiUrlQuestion !== null || openImport !== null;

    // Use AI Model health check hook with pause support
    const {
        isEnabled: healthCheckEnabled,
        isPaused: healthCheckPaused,
        intervalSeconds,
        lastCheckTime,
        isChecking: isAutoChecking,
        runHealthCheck: triggerManualCheck,
        healthCheckConfig,
        refreshConfig,
        checkFailure,
        configFailure,
    } = useAIModelHealthCheck({ paused: isAnyFormOpen });

    // Get actual interval from health check config
    const actualIntervalSeconds = healthCheckConfig?.ai_settings?.interval_seconds ?? intervalSeconds;

    const [refreshing, setRefreshing] = useState(false);

    const loadModels = async () => {
        try {
            if (!hasLoadedRef.current) setLoading(true);
            setRefreshing(true);
            const response = await gatewayClient.listLLMs();
            setModels(response.models || {});
        } catch (error) {
            console.error('Failed to load LLM models:', errorKind(error));
            toast.error(failureSentence('Failed to load LLM models', error));
        } finally {
            setLoading(false);
            setRefreshing(false);
            hasLoadedRef.current = true;
        }
    };

    useEffect(() => {
        runBackground(loadModels(), 'models');
    }, []);

    // Lazily load the global catalog the first time the Browse modal opens.
    //
    // The in-flight/loaded guard lives in a REF, not in state or the dep
    // array. An effect that depends on `catalogLoading` and flips it to
    // `true` as its first act re-runs itself: its cleanup sets
    // `cancelled = true`, so when the fetch finally resolves both the "got
    // data" and "clear spinner" branches are skipped — a permanent spinner
    // on every Browse click, with no error and no recovery short of
    // remounting the tab. Keep effect-owned state out of these deps.
    const catalogFetchRef = useRef(false);

    const loadCatalog = useCallback(async () => {
        if (catalogFetchRef.current) return;
        catalogFetchRef.current = true;
        setCatalogLoading(true);
        setCatalogError(null);
        try {
            // The endpoint caps `limit` at 500 (a larger value 422s), so
            // page through the full catalog rather than asking for 1000.
            const PAGE = 500;
            const all: LLMCatalogEntry[] = [];
            for (let offset = 0; ; offset += PAGE) {
                const res = await gatewayClient.listLLMCatalog({ limit: PAGE, offset });
                all.push(...res.catalog);
                if (res.catalog.length < PAGE || all.length >= (res.total ?? all.length)) break;
            }
            setCatalogEntries(all);
        } catch (e: unknown) {
            const msg = e instanceof Error ? e.message : String(e);
            // Surface it in the modal as well as a toast — a toast alone is
            // indistinguishable from a hang once it auto-dismisses.
            setCatalogError(msg);
            toast.error(`Failed to load model catalog: ${msg}`);
            // Allow an explicit retry after a failure.
            catalogFetchRef.current = false;
        } finally {
            setCatalogLoading(false);
        }
    }, [gatewayClient]);

    useEffect(() => {
        if (!showBrowseModal) return;
        void loadCatalog();
    }, [showBrowseModal, loadCatalog]);

    // Deep-link focus support. When the parent passes
    // `initialFocusModelId` (e.g. Mission Control's "click an alert
    // to jump to the offending model" flow), we wait until the model
    // catalogue has loaded, then mark that model as selected and
    // scroll its row into view. The empty-models early return on
    // the first effect tick is intentional — without it we'd run
    // the focus logic against an empty list and silently no-op.
    const hasFocusedRef = useRef(false);
    useEffect(() => {
        if (!initialFocusModelId) return;
        if (hasFocusedRef.current) return;
        if (!models || Object.keys(models).length === 0) return;
        if (!models[initialFocusModelId]) {
            // Stale alert: the model the alert references has been
            // deleted since the alert was raised. Don't keep retrying.
            hasFocusedRef.current = true;
            return;
        }
        hasFocusedRef.current = true;
        setSelectedModelId(initialFocusModelId);
        // Scroll on the next paint so the row exists in the DOM.
        requestAnimationFrame(() => {
            const el = document.querySelector(`[data-llm-model-id="${initialFocusModelId}"]`);
            if (el && el instanceof HTMLElement) {
                el.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
        });
    }, [initialFocusModelId, models]);

    // Reload models and update display after health checks complete.
    // lastCheckTime is only set after a REAL health check completes
    // (the hook does not set it on mount), so every change is a real check.
    const prevCheckTimeRef = useRef<Date | null>(null);
    useEffect(() => {
        if (!lastCheckTime) return;
        if (prevCheckTimeRef.current === lastCheckTime) return; // Same value, skip
        prevCheckTimeRef.current = lastCheckTime;
        setTimeDisplayKey(prev => prev + 1);
        runBackground(loadModels(), 'models');
    }, [lastCheckTime]);

    // Run the post-save health check in the background and surface the
    // outcome via toast. Operators have repeatedly asked NOT to be
    // blocked by a modal here — saving a model is a pause-and-think
    // moment, but waiting for the upstream provider's first probe to
    // round-trip can take several seconds and shouldn't gate the next
    // action (assigning the model to a group, opening API tab, etc).
    //
    // The mode comes from the caller, not from `models`: this runs from a timer
    // whose closure holds the `models` of the render that saved, where a model
    // just created does not exist yet and a just-edited mode is still the old one.
    const runPostSaveHealthCheck = async (modelId: string, modelName: string, mode: string | null) => {
        const inFlightId = toast.loading(`Running health check: ${modelName}…`);
        try {
            // Pass the model mode so the client uses a long-enough
            // fetch timeout for image / video / audio probes (the
            // 30 s default would abort DALL-E or Imagen mid-call).
            const result: HealthCheckResponse = await gatewayClient.testLLMConnection(modelId, mode);
            if (result.status === 'healthy') {
                const latency = result.response_time_ms?.toFixed(0);
                toast.success(
                    latency
                        ? `Health check passed: ${modelName} (${latency}ms)`
                        : `Health check passed: ${modelName}`,
                    { id: inFlightId },
                );
            } else {
                toast.error(
                    `Health check failed: ${modelName} — ${modelErrorText(result.error_message, 'Connection failed')}`,
                    { id: inFlightId, duration: 7000 },
                );
            }
            await loadModels();
        } catch (error) {
            toast.error(
                `Health check failed: ${modelName} — ${modelErrorText(errorMessage(error, 'unknown error'), 'unknown error')}`,
                { id: inFlightId, duration: 7000 },
            );
        }
    };

    const handleCreateModel = async (modelData: LLModelFormData) => {
        try {
            // Refused here, the save is rejected: the form shows the reason
            // and stays open. A plain `return` would tell the form the model
            // was created.
            if (!modelData.name || !modelData.provider || !modelData.model_name) {
                throw new Error('Name, provider and model name are required. Nothing was created.');
            }

            // An address is needed unless the gateway finds it.
            if (!modelData.api_url_auto_discovered && !modelData.api_url) {
                throw new Error('API URL is required when Auto-discover API URL is off. Nothing was created.');
            }

            // Build create payload from all form data
            const createPayload: LLModelCreateRequest = {
                ...modelData as LLModelCreateRequest,
                enabled: modelData.enabled ?? true,
            };

            // Remove fields that shouldn't go to create endpoint
            delete (createPayload as any).id;
            delete (createPayload as any).health_status;
            delete (createPayload as any).performance_metrics;
            delete (createPayload as any).created_at;
            delete (createPayload as any).updated_at;
            delete (createPayload as any).groups;

            const created = await gatewayClient.createLLM(createPayload);
            toast.success(`LLM Model "${modelData.name}" created successfully`);
            setIsCreatingNew(false);
            setNewModelPrefill(null);
            await loadModels();
            // Auto health check after create — delay to let proxy cache refresh
            if (created?.id) {
                const mode = created.mode ?? modelData.mode ?? null;
                setTimeout(() => runBackground(runPostSaveHealthCheck(created.id, modelData.name || 'Model', mode), 'post save health check'), 2000);
            }
        } catch (error) {
            console.error('Create model error:', errorKind(error));
            throw saveFailure(error, 'Failed to create model');
        }
    };

    const handleUpdateModel = async (modelId: string, updates: LLModelFormData) => {
        try {
            const modelName = updates.name || editingModel?.name || models[modelId]?.name || 'Model';

            // The three fields of a catalog entry are not a model's.
            const { recommended, deprecated, icon_url, ...body } = updates;
            await gatewayClient.updateLLM(modelId, body);
            toast.success(`LLM Model "${modelName}" updated successfully`);
            setEditingModel(null);
            await loadModels();
            // Auto health check after update — delay to let proxy cache refresh
            const mode = updates.mode ?? models[modelId]?.mode ?? null;
            setTimeout(() => runBackground(runPostSaveHealthCheck(modelId, modelName, mode), 'post save health check'), 2000);
        } catch (error) {
            console.error('Update model error:', errorKind(error));
            throw saveFailure(error, 'Failed to update model');
        }
    };

    const handleDeleteModel = (modelId: string) => {
        const model = models[modelId];
        if (!model) {
            toast.error('Model not found');
            return;
        }

        setDeleteConfirmation({
            modelId,
            modelName: model.name,
            modelInfo: model,
        });
        setDeleteError(null);
    };

    const confirmDeleteModel = async () => {
        if (!deleteConfirmation) return;

        setIsDeleting(true);
        try {
            await gatewayClient.deleteLLM(deleteConfirmation.modelId);
            toast.success(`LLM Model "${deleteConfirmation.modelName}" deleted successfully`);
            if (selectedModelId === deleteConfirmation.modelId) {
                setSelectedModelId(null);
            }
            setDeleteConfirmation(null);
            await loadModels();
        } catch (error) {
            setDeleteError(errorMessage(error, 'Failed to delete model'));
        } finally {
            setIsDeleting(false);
        }
    };

    const handleTestConnection = async (modelId: string) => {
        setTestingConnections(prev => new Set(prev).add(modelId));

        try {
            // Pass the model mode so the client uses a long-enough
            // fetch timeout for image / video / audio probes (the
            // 30 s default would abort DALL-E or Imagen mid-call).
            const modeForTimeout = models[modelId]?.mode ?? null;
            const result: HealthCheckResponse = await gatewayClient.testLLMConnection(modelId, modeForTimeout);

            if (result.status === 'healthy') {
                toast.success(`Connection successful! Latency: ${result.response_time_ms?.toFixed(0)}ms`);
            } else {
                toast.error(
                    `Connection failed: ${modelErrorText(result.error_message, 'Unknown error')}`,
                    { duration: 5000 }
                );
            }

            await loadModels();
        } catch (error) {
            toast.error(`Connection test failed: ${modelErrorText(errorMessage(error), 'Unknown error')}`);
        } finally {
            setTestingConnections(prev => {
                const newSet = new Set(prev);
                newSet.delete(modelId);
                return newSet;
            });
        }
    };

    const handleToggleEnabled = async (modelId: string, enabled: boolean) => {
        const modelName = models[modelId]?.name || 'Model';
        setTogglingModel({ name: modelName, enabling: enabled });
        try {
            await gatewayClient.updateLLM(modelId, { enabled });
            toast.success(`LLM Model "${modelName}" ${enabled ? 'enabled' : 'disabled'} successfully`);
            await loadModels();
        } catch (error) {
            toast.error(`Failed to update model: ${errorMessage(error)}`);
        } finally {
            setTogglingModel(null);
        }
    };

    // Import a model from the GLOBAL catalog (Browse / Search modals). Materialises
    // a configured `llm_model` instance, then reloads the configured list
    // and surfaces the post-import setup notice: the model's credential is
    // next; it is already bound to the Default group.
    const importCatalogEntry = async (entry: LLMCatalogEntry, overrides?: LLMCatalogImportRequest) => {
        const created = overrides
            ? await gatewayClient.importLLMCatalogModel(entry.id, overrides)
            : await gatewayClient.importLLMCatalogModel(entry.id);
        const cloud = isBedrockProvider(created?.provider)
            ? 'Amazon Bedrock'
            : isVertexProvider(created?.provider)
              ? 'Vertex AI'
              : undefined;
        const addedDisabledOn = created?.enabled === false ? cloud : undefined;
        toast.success(addedDisabledOn ? 'Model added, disabled' : 'Model added');
        setShowSearchModal(false);
        await loadModels();
        if (created?.id) {
            setEnabledNotice({ modelId: created.id, modelName: created.name, addedDisabledOn });
        }
    };

    // An entry the control plane marks `requires_api_url` is imported with
    // the address of the administrator's own endpoint: the question is asked
    // first (LLMCatalogApiUrlDialog) and the promise settles when it is
    // answered or cancelled, so the row that started it stops spinning.
    const askForApiUrl = (entry: LLMCatalogEntry, refusal?: string) =>
        new Promise<void>((done) => setApiUrlQuestion({ entry, refusal, done }));

    const handleCatalogImport = async (entry: LLMCatalogEntry) => {
        if (entry.requires_api_url) return askForApiUrl(entry);
        try {
            await importCatalogEntry(entry);
        } catch (error) {
            // The control plane says the entry needs an address the list did
            // not say it needs (a list read before the entry changed).
            const refusal = apiUrlRefusal(error);
            if (refusal) return askForApiUrl(entry, refusal);
            console.error('Import model error:', errorKind(error));
            const said = modelSaveRefusalText(error) ?? (error instanceof Error ? error.message : 'Failed to add model');
            toast.error(`Failed to add model: ${said}`);
            throw error;
        }
    };

    const handleCopyModel = async (sourceModel: LLModelConfig) => {
        setCopyingModel(sourceModel);
        try {
            // Server-side copy — the API never returns the plaintext
            // api_key, so duplicating client-side would either drop
            // the key or require an admin reveal endpoint. Doing it
            // on the server also guarantees every column on the
            // source row survives (parameters, supports_batch,
            // max_concurrent_requests, additional_drop_params, …)
            // without this component having to enumerate them.
            const result = await gatewayClient.copyLLM(sourceModel.id);
            toast.success(`Model copied as "${result.model.name}"`);
            await loadModels();
        } catch (error) {
            console.error('Copy model error:', errorKind(error));
            toast.error(`Failed to copy model: ${saveFailure(error, 'Unknown error').message.replace(/\n/g, '; ')}`);
        } finally {
            setCopyingModel(null);
        }
    };

    // Sort helpers — extract a comparable value for each row so a
    // single generic comparator handles every column.
    const sortValue = (m: LLModelConfig, key: SortKey): string | number => {
        switch (key) {
            case 'name':
                return (m.name || '').toLowerCase();
            case 'provider':
                return `${(m.provider || '').toLowerCase()} ${(m.name || '').toLowerCase()}`;
            case 'size':
                // Missing size is handled in the comparator below.
                return typeof m.parameters === 'number' ? m.parameters : -1;
            case 'requests':
                return m.performance_metrics?.total_requests ?? 0;
            case 'tokens':
                return m.performance_metrics?.total_tokens_consumed ?? 0;
            case 'updated':
                return m.updated_at ? Date.parse(m.updated_at) : 0;
        }
    };
    const modelList = Object.values(models).slice().sort((a, b) => {
        // Missing size sinks to the bottom regardless of direction — "unknown"
        // shouldn't float above real numbers in ascending.
        if (sortBy === 'size') {
            const aMissing = typeof a.parameters !== 'number';
            const bMissing = typeof b.parameters !== 'number';
            if (aMissing !== bMissing) return aMissing ? 1 : -1;
        }
        const av = sortValue(a, sortBy);
        const bv = sortValue(b, sortBy);
        let cmp: number;
        if (typeof av === 'number' && typeof bv === 'number') {
            cmp = av - bv;
        } else {
            cmp = String(av).localeCompare(String(bv));
        }
        return sortDir === 'asc' ? cmp : -cmp;
    });
    const enabledModels = modelList.filter(m => m.enabled);
    // Models discovered via Traffic Data Import — observability-only (disabled)
    // until an operator promotes them. Surfaced in their own section so the
    // estate-wide model inventory is visible, not hidden behind the enabled set.
    //
    // This section stays in the Community tab; it is not behind a
    // slot. It is a filter over the Community model list (`listLLMs`,
    // `/v1/admin/llms`) on two Community columns (`source`, `enabled`), drawn
    // with the tab's own rows and handlers; it calls no method of an Enterprise
    // API module and imports no Enterprise component. Only the Enterprise
    // traffic import writes `source = 'import'` (control plane,
    // services/usage_import/resolver.py), so without it there are no such rows
    // and the section is not drawn. Held by LLModelTab.test.tsx ('the observed
    // models section') and app/__tests__/enterpriseApiCallers.test.ts.
    const importedModels = modelList.filter(m => m.source === 'import' && !m.enabled);
    // Every other model that is switched off: one disabled here, and one
    // that arrived disabled (a catalog entry on Amazon Bedrock or Vertex AI,
    // which has no credential yet). Listed, or it could not be given what
    // it lacks, enabled again or deleted.
    const disabledModels = modelList.filter(m => m.source !== 'import' && !m.enabled);
    const totalModels = modelList.length;
    const enabledCount = enabledModels.length;
    const providerCount = new Set(modelList.map(m => m.provider)).size;

    if (loading) {
        return (
            <div className="flex items-center justify-center h-96">
                <RefreshCw className="w-8 h-8 animate-spin text-primary-600" />
            </div>
        );
    }

    return (
        <div className="app-column h-full">
            {/* ───────── Action Bar ───────── */}
            <div className="flex-shrink-0 px-6 py-4 bg-surface border-b border-default space-y-3">

                {/* Row 1: Catalog + Add New */}
                <div className="flex items-center justify-between">
                    {/* Left: Enable from catalog + HuggingFace */}
                    <div className="flex items-center space-x-3">
                        <div className="flex items-center space-x-1">
                            <span className="text-sm font-medium text-secondary mr-2">Enable models from catalog</span>
                            <div className="flex items-center rounded-lg border border-strong overflow-hidden">
                                <button
                                    onClick={() => setShowBrowseModal(true)}
                                    className="flex items-center space-x-1.5 px-3 py-2 text-sm font-medium text-secondary bg-surface hover:bg-surface-alt transition-colors border-r border-strong"
                                >
                                    <FolderTree className="w-4 h-4" />
                                    <span>Browse</span>
                                </button>
                                <button
                                    onClick={() => setShowSearchModal(true)}
                                    className="flex items-center space-x-1.5 px-3 py-2 text-sm font-medium text-secondary bg-surface hover:bg-surface-alt transition-colors"
                                >
                                    <Search className="w-4 h-4" />
                                    <span>Search</span>
                                </button>
                            </div>
                        </div>
                        {/* Other places to find a model, contributed to the
                            `llm-models.import` slot (the HuggingFace search is). */}
                        {importSources.map((source) => (
                            <source.Button key={source.id} onOpen={() => setOpenImport(source.id)} />
                        ))}
                    </div>

                    {/* Right: Add New Model */}
                    <button
                        onClick={() => setIsCreatingNew(true)}
                        className="flex items-center space-x-2 px-4 py-2 bg-primary-500 hover:bg-primary-600 text-white rounded-lg transition-colors text-sm font-medium"
                    >
                        <Plus className="w-4 h-4" />
                        <span>Add New Model</span>
                    </button>
                </div>

                <ReadFailureNotice compact failure={checkFailure} onRetry={asyncHandler(triggerManualCheck, 'health check')} />
                <ReadFailureNotice compact failure={configFailure} onRetry={asyncHandler(refreshConfig, 'health check settings', 'background')} />
                {/* Row 2: Health Check + Utilities */}
                <div className="flex items-center justify-between">
                    {/* Left: Health Check Status */}
                    <div className={`flex items-center space-x-2 px-3 py-1.5 rounded-lg text-sm ${
                        healthCheckPaused
                            ? 'bg-yellow-50 border border-yellow-200'
                            : healthCheckEnabled
                                ? 'bg-blue-50 border border-blue-200'
                                : 'bg-surface-alt border border-default'
                    }`}>
                        {healthCheckPaused ? (
                            <>
                                <PauseCircle className="w-4 h-4 text-yellow-600" />
                                <span className="text-secondary">Health check paused</span>
                            </>
                        ) : healthCheckEnabled ? (
                            <>
                                <Activity className={`w-4 h-4 text-green-600 ${isAutoChecking ? 'animate-pulse' : ''}`} />
                                <span className="text-secondary">
                                    Health check every {actualIntervalSeconds}s
                                    {lastCheckTime && (
                                        <span key={timeDisplayKey}>
                                            {' • Last check: '}
                                            {lastCheckTime.toLocaleTimeString(undefined, {
                                                hour: '2-digit',
                                                minute: '2-digit',
                                                second: '2-digit'
                                            })}
                                        </span>
                                    )}
                                </span>
                            </>
                        ) : (
                            <>
                                <Activity className="w-4 h-4 text-muted" />
                                <span className="text-muted">Health check disabled</span>
                            </>
                        )}

                        {/* Divider */}
                        <div className="w-px h-4 bg-surface-inset" />

                        <button
                            onClick={asyncHandler(async () => { await triggerManualCheck(); await loadModels(); }, 'click')}
                            disabled={isAutoChecking}
                            className="flex items-center space-x-1 px-2 py-1 text-xs font-medium bg-surface border border-strong rounded hover:bg-surface-alt transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                            title="Run health checks now"
                        >
                            <RefreshCw className={`w-3 h-3 ${isAutoChecking ? 'animate-spin' : ''}`} />
                            <span>{isAutoChecking ? 'Checking...' : 'Check Now'}</span>
                        </button>
                        <button
                            onClick={() => setShowHealthHelp(true)}
                            className="flex items-center space-x-1 px-2 py-1 text-xs font-medium bg-surface border border-strong rounded hover:bg-surface-alt transition-colors"
                            title="Learn how Brutor probes different model types"
                        >
                            <HelpCircle className="w-3 h-3" />
                            <span>How it works</span>
                        </button>
                    </div>

                    {/* Right: Refresh + Proxy Info */}
                    <div className="flex items-center rounded-lg border border-strong overflow-hidden">
                        <button
                            onClick={asyncHandler(loadModels, 'click')}
                            disabled={refreshing}
                            className="flex items-center space-x-1.5 px-3 py-2 text-sm font-medium text-secondary bg-surface hover:bg-surface-alt transition-colors disabled:opacity-50 border-r border-strong"
                        >
                            <RefreshCw className={`w-3.5 h-3.5 ${refreshing ? 'animate-spin' : ''}`} />
                            <span>{refreshing ? 'Refreshing...' : 'Refresh'}</span>
                        </button>
                        <button
                            onClick={() => setShowProxyModal(true)}
                            className="flex items-center space-x-1.5 px-3 py-2 text-sm font-medium text-secondary bg-surface hover:bg-surface-alt transition-colors"
                            title="View LLM Proxy Endpoint information"
                        >
                            <Link className="w-3.5 h-3.5" />
                            <span>Proxy API Access</span>
                        </button>
                    </div>
                </div>
            </div>

            {/* ───────── Main Content ───────── */}
            <div className="flex-1 bg-surface-alt overflow-hidden flex flex-col min-h-0 min-w-0">
                {/* Content Header: Count + Layout Toggle */}
                <div className="flex-shrink-0 px-6 py-3 border-b border-default bg-surface flex items-center justify-between">
                    <div>
                        <h3 className="font-semibold text-default">
                            Enabled Models
                            <span className="ml-2 text-sm font-normal text-muted">
                                {enabledCount} of {totalModels} active across {providerCount} provider{providerCount !== 1 ? 's' : ''}
                            </span>
                        </h3>
                    </div>

                    {/* Sort + Layout controls — grouped so they share the
                        same visual affordance as a single toolbar. */}
                    {enabledModels.length > 0 && (
                        <div className="flex items-center space-x-3">
                            <label className="text-sm text-muted">Sort by</label>
                            <select
                                value={sortBy}
                                onChange={(e) => setSortBy(e.target.value as SortKey)}
                                className="text-sm border border-strong rounded-lg bg-surface px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-primary-500"
                            >
                                <option value="name">Name</option>
                                <option value="provider">Provider</option>
                                <option value="size">Size (parameters)</option>
                                <option value="requests">Most requests</option>
                                <option value="tokens">Most tokens</option>
                                <option value="updated">Last updated</option>
                            </select>
                            <button
                                onClick={() => setSortDir(sortDir === 'asc' ? 'desc' : 'asc')}
                                className="p-2 rounded border border-strong bg-surface hover:bg-surface-alt text-secondary transition-colors"
                                title={sortDir === 'asc' ? 'Ascending — click to reverse' : 'Descending — click to reverse'}
                                aria-label={`Toggle sort direction (currently ${sortDir})`}
                            >
                                {sortDir === 'asc' ? '↑' : '↓'}
                            </button>

                            <div className="flex items-center space-x-1 bg-surface-raised rounded-lg p-1">
                                <button
                                    onClick={() => setLayout('card')}
                                    className={`p-2 rounded transition-colors ${
                                        layout === 'card'
                                            ? 'bg-surface text-primary-600 shadow-sm'
                                            : 'text-secondary hover:text-default'
                                    }`}
                                    title="Card view"
                                >
                                    <LayoutGrid className="w-4 h-4" />
                                </button>
                                <button
                                    onClick={() => setLayout('list')}
                                    className={`p-2 rounded transition-colors ${
                                        layout === 'list'
                                            ? 'bg-surface text-primary-600 shadow-sm'
                                            : 'text-secondary hover:text-default'
                                    }`}
                                    title="List view"
                                >
                                    <List className="w-4 h-4" />
                                </button>
                            </div>
                        </div>
                    )}
                </div>

                {/* Content Body */}
                <div className="scroll-region p-6">
                    {enabledModels.length === 0 ? (
                        <div className={`flex items-center justify-center ${importedModels.length + disabledModels.length > 0 ? 'py-12' : 'h-full'}`}>
                            <div className="text-center">
                                <AlertCircle className="w-16 h-16 text-muted mx-auto mb-4" />
                                <h3 className="text-lg font-semibold text-default mb-2">No Enabled Models</h3>
                                <p className="text-secondary mb-6">
                                    Browse the catalog or add a new model to get started
                                </p>
                                <div className="flex items-center justify-center space-x-3">
                                    <button
                                        onClick={() => setShowBrowseModal(true)}
                                        className="inline-flex items-center space-x-2 px-5 py-2.5 border border-strong text-secondary rounded-lg hover:bg-surface-alt transition-colors"
                                    >
                                        <FolderTree className="w-4 h-4" />
                                        <span>Browse Catalog</span>
                                    </button>
                                    <button
                                        onClick={() => setIsCreatingNew(true)}
                                        className="inline-flex items-center space-x-2 px-5 py-2.5 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors shadow-sm"
                                    >
                                        <Plus className="w-5 h-5" />
                                        <span>Add New Model</span>
                                    </button>
                                </div>
                            </div>
                        </div>
                    ) : layout === 'card' ? (
                        <div className="grid grid-cols-1 xl:grid-cols-2 2xl:grid-cols-3 gap-4">
                            {enabledModels.map(model => (
                                <LLModelCard
                                    key={model.id}
                                    model={model}
                                    onEdit={() => setEditingModel(model)}
                                    onDelete={() => handleDeleteModel(model.id)}
                                    onCopy={asyncHandler(() => handleCopyModel(model), 'copy')}
                                    onTestConnection={asyncHandler(() => handleTestConnection(model.id), 'test connection')}
                                    onToggleEnabled={asyncHandler((enabled) => handleToggleEnabled(model.id, enabled), 'toggle enabled')}
                                    isTesting={testingConnections.has(model.id)}
                                    isFocused={selectedModelId === model.id}
                                />
                            ))}
                        </div>
                    ) : (
                        <div className="bg-surface rounded-lg border border-default">
                            {/* Table Header */}
                            <div className={`${TABLE_TH_TYPE} hidden lg:flex items-center px-4 py-3 bg-surface-alt border-b border-default`}>
                                <div className="flex-1 min-w-0 min-h-0">Model</div>
                                <div className="w-24 text-center flex-shrink-0">Mode</div>
                                <div className="w-20 text-center flex-shrink-0">Size</div>
                                <div className="w-16 text-center flex-shrink-0">Context</div>
                                <div className="flex items-center space-x-3 mx-3 flex-shrink-0">
                                    <div className="text-center w-16">Success</div>
                                    <div className="text-center w-16">Latency</div>
                                    <div className="text-center w-16">Requests</div>
                                    <div className="text-left min-w-[250px] max-w-[350px]">Access</div>
                                </div>
                                <div className="w-24 text-right flex-shrink-0">Actions</div>
                            </div>

                            {/* Table Body */}
                            {enabledModels.map(model => (
                                <LLModelListItem
                                    key={model.id}
                                    model={model}
                                    onEdit={() => setEditingModel(model)}
                                    onDelete={() => handleDeleteModel(model.id)}
                                    onCopy={asyncHandler(() => handleCopyModel(model), 'copy')}
                                    onTestConnection={asyncHandler(() => handleTestConnection(model.id), 'test connection')}
                                    onToggleEnabled={asyncHandler((enabled) => handleToggleEnabled(model.id, enabled), 'toggle enabled')}
                                    isTesting={testingConnections.has(model.id)}
                                    isFocused={selectedModelId === model.id}
                                />
                            ))}
                        </div>
                    )}

                    {/* ───────── Observed (imported) models ───────── */}
                    {importedModels.length > 0 && (
                        <div className="mt-8">
                            <div className="flex items-center mb-3">
                                <h3 className="font-semibold text-default flex items-center">
                                    <Download className="w-4 h-4 mr-2 text-primary-600" />
                                    Observed (imported) models
                                    <span className="ml-2 text-sm font-normal text-muted">
                                        {importedModels.length} discovered via Traffic Data Import
                                    </span>
                                </h3>
                            </div>
                            <p className="text-sm text-secondary mb-3">
                                Discovered from vendor traffic the gateway doesn&apos;t proxy
                                (e.g. desktop/web chat). They&apos;re tracked for cost &amp; usage
                                visibility but stay <span className="font-medium">disabled</span> —
                                enable one to govern it inline going forward.
                            </p>
                            <div className="bg-surface rounded-lg border border-default">
                                {importedModels.map(model => (
                                    <LLModelListItem
                                        key={model.id}
                                        model={model}
                                        onEdit={() => setEditingModel(model)}
                                        onDelete={() => handleDeleteModel(model.id)}
                                        onCopy={asyncHandler(() => handleCopyModel(model), 'copy')}
                                        onTestConnection={asyncHandler(() => handleTestConnection(model.id), 'test connection')}
                                        onToggleEnabled={asyncHandler((enabled) => handleToggleEnabled(model.id, enabled), 'toggle enabled')}
                                        isTesting={testingConnections.has(model.id)}
                                        isFocused={selectedModelId === model.id}
                                    />
                                ))}
                            </div>
                        </div>
                    )}

                    {/* ───────── Disabled models ───────── */}
                    {disabledModels.length > 0 && (
                        <div className="mt-8" data-testid="disabled-models">
                            <div className="flex items-center mb-3">
                                <h3 className="font-semibold text-default flex items-center">
                                    <Power className="w-4 h-4 mr-2 text-muted" />
                                    Disabled models
                                    <span className="ml-2 text-sm font-normal text-muted">
                                        {disabledModels.length} switched off
                                    </span>
                                </h3>
                            </div>
                            <p className="text-sm text-secondary mb-3">
                                The gateway sends no request for a model that is switched off. A model on Amazon
                                Bedrock or Vertex AI is added disabled: edit it, enter its credential, then enable it.
                            </p>
                            <div className="bg-surface rounded-lg border border-default">
                                {disabledModels.map(model => (
                                    <LLModelListItem
                                        key={model.id}
                                        model={model}
                                        onEdit={() => setEditingModel(model)}
                                        onDelete={() => handleDeleteModel(model.id)}
                                        onCopy={asyncHandler(() => handleCopyModel(model), 'copy')}
                                        onTestConnection={asyncHandler(() => handleTestConnection(model.id), 'test connection')}
                                        onToggleEnabled={asyncHandler((enabled) => handleToggleEnabled(model.id, enabled), 'toggle enabled')}
                                        isTesting={testingConnections.has(model.id)}
                                        isFocused={selectedModelId === model.id}
                                    />
                                ))}
                            </div>
                        </div>
                    )}
                </div>
            </div>

            {/* ───────── Browse Catalog Modal ───────── */}
            {showBrowseModal && (
                <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
                    <div className="bg-surface rounded-lg shadow-xl w-full max-w-2xl mx-4 max-h-[85vh] flex flex-col">
                        {/* Header */}
                        <div className="flex items-center justify-between px-5 py-4 border-b border-default">
                            <div>
                                <h2 className="text-lg font-semibold text-default">Browse Model Catalog</h2>
                                <p className="text-xs text-muted mt-0.5">
                                    {catalogError
                                        ? 'Could not load the global model catalog'
                                        : catalogEntries.length > 0
                                            ? `${catalogEntries.length} models available — add one to configure it as an instance`
                                            : 'Loading the global model catalog…'}
                                </p>
                            </div>
                            <button
                                onClick={() => setShowBrowseModal(false)}
                                className="p-1.5 text-muted hover:text-secondary hover:bg-surface-raised rounded-lg transition-colors"
                            >
                                <X className="w-5 h-5" />
                            </button>
                        </div>
                        <p data-testid="catalog-cloud-note" className="flex-shrink-0 px-5 py-2 text-xs text-secondary border-b border-default bg-surface-alt">
                            A model on Amazon Bedrock or Vertex AI is added disabled. It has no credential yet: edit it,
                            enter its credential, then enable it.
                        </p>

                        {/* Catalog Tree — browse the GLOBAL catalog and import. */}
                        <div className="scroll-region">
                            {catalogError && !catalogLoading ? (
                                // Without this branch a failed load is
                                // indistinguishable from a hang: the tree only
                                // knows "loading" and "empty".
                                <div className="p-6 text-center">
                                    <AlertCircle className="w-8 h-8 text-red-500 mx-auto mb-3" />
                                    <p className="text-sm text-default font-medium">
                                        Could not load the model catalog
                                    </p>
                                    <p className="text-xs text-muted mt-1 break-words">{catalogError}</p>
                                    <button
                                        onClick={() => void loadCatalog()}
                                        className="mt-4 inline-flex items-center gap-2 px-3 py-2 text-sm font-medium text-secondary border border-strong rounded-lg hover:bg-surface-alt transition-colors"
                                    >
                                        <RefreshCw className="w-4 h-4" /> Retry
                                    </button>
                                </div>
                            ) : (
                            <LLMCatalogTree
                                entries={catalogEntries}
                                loading={catalogLoading}
                                configuredKeys={new Set(
                                    Object.values(models).map(m => `${m.provider}:${m.model_name}`),
                                )}
                                onImport={handleCatalogImport}
                            />
                            )}
                        </div>

                        {/* Footer */}
                        <div className="flex-shrink-0 px-5 py-3 border-t border-default bg-surface-alt rounded-b-lg flex justify-end">
                            <button
                                onClick={() => setShowBrowseModal(false)}
                                className="px-4 py-2 text-sm font-medium text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors"
                            >
                                Close
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* ───────── Search Modal — browse the global catalog & import ───────── */}
            {showSearchModal && (
                <LLMSearchModal
                    isOpen={true}
                    onClose={() => setShowSearchModal(false)}
                    onImportModel={handleCatalogImport}
                />
            )}

            {/* The API URL of a catalog entry that needs one, asked before the import. */}
            {apiUrlQuestion && (
                <LLMCatalogApiUrlDialog
                    entry={apiUrlQuestion.entry}
                    refusal={apiUrlQuestion.refusal}
                    onImport={async (apiUrl) => {
                        await importCatalogEntry(apiUrlQuestion.entry, { api_url: apiUrl });
                        apiUrlQuestion.done();
                        setApiUrlQuestion(null);
                    }}
                    onCancel={() => {
                        apiUrlQuestion.done();
                        setApiUrlQuestion(null);
                    }}
                />
            )}

            {/* The dialogs of the import sources. Always mounted. */}
            {importSources.map((source) => (
                <source.Dialog
                    key={source.id}
                    isOpen={openImport === source.id}
                    onClose={() => setOpenImport(null)}
                    onCreateModel={(prefill) => {
                        setOpenImport(null);
                        setNewModelPrefill(prefill);
                        setIsCreatingNew(true);
                    }}
                />
            ))}

            {/* Create New Model Modal */}
            {isCreatingNew && (
                <LLModelForm
                    model={newModelPrefill as LLModelConfig | null}
                    onClose={() => { setIsCreatingNew(false); setNewModelPrefill(null); }}
                    onSave={handleCreateModel}
                />
            )}

            {/* Edit Model Modal */}
            {editingModel && (
                <LLModelForm
                    model={editingModel}
                    onClose={() => setEditingModel(null)}
                    onSave={(updates) => handleUpdateModel(editingModel.id, updates)}
                />
            )}

            {/* Health Check Config Modal */}

            {/* Health Check Help Modal — operator-facing reference */}
            <HealthCheckHelpModal
                isOpen={showHealthHelp}
                onClose={() => setShowHealthHelp(false)}
            />

            {/* LLM Proxy Endpoint Modal */}
            <LLMProxyEndpointModal
                isOpen={showProxyModal}
                onClose={() => setShowProxyModal(false)}
                proxyUrl={llmProxyUrl}
            />

            {/* Delete Confirmation Modal */}
            <GenericDeleteConfirmationModal
                isOpen={deleteConfirmation !== null}
                onClose={() => { setDeleteConfirmation(null); setDeleteError(null); }}
                onConfirm={asyncHandler(confirmDeleteModel, 'confirm')}
                itemType="model"
                itemName={deleteConfirmation?.modelName || ''}
                itemInfo={deleteConfirmation?.modelInfo ? {
                    id: deleteConfirmation.modelInfo.id,
                    provider: deleteConfirmation.modelInfo.provider,
                    model_name: deleteConfirmation.modelInfo.model_name,
                    description: deleteConfirmation.modelInfo.description,
                    enabled: deleteConfirmation.modelInfo.enabled,
                } : undefined}
                isDeleting={isDeleting}
                errorMessage={deleteError}
            />

            {/* Toggling model — hourglass overlay */}
            {togglingModel && (
                <div className="fixed inset-0 bg-black bg-opacity-40 flex items-center justify-center z-50">
                    <div className="bg-surface rounded-lg shadow-xl px-6 py-5 flex items-center space-x-3">
                        <RefreshCw className="w-5 h-5 text-primary-600 animate-spin" />
                        <span className="text-sm font-medium text-secondary">
                            {togglingModel.enabling ? 'Enabling' : 'Disabling'} model…
                        </span>
                    </div>
                </div>
            )}

            {isDeleting && (
                <div className="fixed inset-0 bg-black bg-opacity-40 flex items-center justify-center z-[60]">
                    <div className="bg-surface rounded-lg shadow-xl px-6 py-5 flex items-center space-x-3">
                        <RefreshCw className="w-5 h-5 text-red-600 animate-spin" />
                        <span className="text-sm font-medium text-secondary">
                            Deleting model…
                        </span>
                    </div>
                </div>
            )}

            {/* Copying model — overlay spinner. Server-side duplication
                takes a moment (encrypted key carry-over, group rebinding,
                proxy cache invalidate) so a loud affordance is warranted. */}
            {copyingModel && (
                <div className="fixed inset-0 bg-black bg-opacity-40 flex items-center justify-center z-[60]">
                    <div className="bg-surface rounded-lg shadow-xl px-6 py-5 flex items-center space-x-3">
                        <RefreshCw className="w-5 h-5 text-primary-600 animate-spin" />
                        <div>
                            <p className="text-sm font-medium text-default">Copying model…</p>
                            <p className="text-xs text-muted mt-0.5">
                                {copyingModel.name}
                            </p>
                        </div>
                    </div>
                </div>
            )}

            {/* Model Enabled Notice Modal */}
            {enabledNotice && (
                <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
                    <div data-testid="model-added-notice" className="bg-surface rounded-lg shadow-xl w-full max-w-md mx-4">
                        {/* Header */}
                        <div className="flex items-center space-x-3 px-5 py-4 border-b border-default">
                            <div className={`w-9 h-9 rounded-full flex items-center justify-center flex-shrink-0 ${enabledNotice.addedDisabledOn ? 'bg-amber-100' : 'bg-green-100'}`}>
                                {enabledNotice.addedDisabledOn ? (
                                    <Power className="w-5 h-5 text-amber-600" />
                                ) : (
                                    <CheckCircle2 className="w-5 h-5 text-green-600" />
                                )}
                            </div>
                            <h3 className="text-lg font-semibold text-default">
                                {enabledNotice.addedDisabledOn ? 'Model added, disabled' : 'Model Ready'}
                            </h3>
                        </div>

                        {/* Body */}
                        <div className="px-5 py-4">
                            {enabledNotice.addedDisabledOn ? (
                                <>
                                    <p className="text-sm text-secondary">
                                        <span className="font-semibold">{enabledNotice.modelName}</span> was added disabled: a model on{' '}
                                        {enabledNotice.addedDisabledOn} has no credential until you enter one. To start using this model:
                                    </p>
                                    <div className="mt-3 space-y-2">
                                        <div className="flex items-start space-x-2.5 text-sm text-secondary">
                                            <Key className="w-4 h-4 text-amber-500 mt-0.5 flex-shrink-0" />
                                            <span>
                                                Enter the{' '}
                                                {enabledNotice.addedDisabledOn === 'Vertex AI'
                                                    ? 'service account JSON key, GCP project ID and GCP location'
                                                    : 'AWS access key ID, secret access key and AWS region'}{' '}
                                                in the API tab
                                            </span>
                                        </div>
                                        <div className="flex items-start space-x-2.5 text-sm text-secondary">
                                            <Power className="w-4 h-4 text-green-600 mt-0.5 flex-shrink-0" />
                                            <span>Enable the model. It is listed under Disabled models until then</span>
                                        </div>
                                        <div className="flex items-start space-x-2.5 text-sm text-secondary">
                                            <Users className="w-4 h-4 text-blue-500 mt-0.5 flex-shrink-0" />
                                            <span>Available to the <span className="font-medium text-default">Default</span> group. To restrict who can use it, edit the model's Access Control tab</span>
                                        </div>
                                    </div>
                                </>
                            ) : (
                                <>
                                    <p className="text-sm text-secondary">
                                        <span className="font-semibold">{enabledNotice.modelName}</span> is ready.
                                        To start using this model:
                                    </p>
                                    <div className="mt-3 space-y-2">
                                        <div className="flex items-start space-x-2.5 text-sm text-secondary">
                                            <Key className="w-4 h-4 text-amber-500 mt-0.5 flex-shrink-0" />
                                            <span>Configure the <span className="font-medium text-default">API Key</span> in the API tab</span>
                                        </div>
                                        <div className="flex items-start space-x-2.5 text-sm text-secondary">
                                            <Users className="w-4 h-4 text-blue-500 mt-0.5 flex-shrink-0" />
                                            <span>Available to the <span className="font-medium text-default">Default</span> group. To restrict who can use it, edit the model's Access Control tab</span>
                                        </div>
                                    </div>
                                </>
                            )}
                            <p className="mt-3 text-xs text-muted">
                                Use the <Edit className="w-3 h-3 inline" /> Edit option from the card menu (⋮) to configure.
                            </p>
                        </div>

                        {/* Footer */}
                        <div className="flex items-center justify-end space-x-3 px-5 py-3 border-t border-default bg-surface-alt rounded-b-lg">
                            <button
                                onClick={() => setEnabledNotice(null)}
                                className="px-4 py-2 text-sm font-medium text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors"
                            >
                                OK
                            </button>
                            <button
                                onClick={() => {
                                    const model = models[enabledNotice.modelId];
                                    setEnabledNotice(null);
                                    setShowBrowseModal(false);
                                    setShowSearchModal(false);
                                    setOpenImport(null);
                                    if (model) setEditingModel(model);
                                }}
                                className="px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors flex items-center space-x-1.5"
                            >
                                <Edit className="w-3.5 h-3.5" />
                                <span>Edit Model</span>
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
};

export default LLModelTab;
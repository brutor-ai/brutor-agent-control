/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import {
    Brain,
    Loader,
    AlertCircle,
    Search,
    X,
    ArrowUpRight,
    Info,
    HelpCircle
} from 'lucide-react';
import { ResourceGroup, LLModelInfo, EffectiveLLModelsResponse, formatGroupLabel } from '../../../lib/groupTypes';
import { useGateway } from '../../../hooks/useGateway';
import { useToast } from '../../common/Toast';
import GenericDeleteConfirmationModal from '../../common/GenericDeleteConfirmationModal';
import GroupLLModelsTableView, { type ModelTableColumn } from './GroupLLModelsTableView';
import LLMGovernanceDialog from './LLMGovernanceDialog';
import LLMModelLimitsDialog from './LLMModelLimitsDialog';
import { errorKind, errorMessage } from '../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import { useSlot } from '../../../app/slots';

interface Props {
    group: ResourceGroup;
    onUpdate: () => void;
}

type ViewMode = 'all' | 'direct' | 'inherited';

const GroupLLModelsTab: React.FC<Props> = ({ group, onUpdate }) => {
    const { fetchEffectiveLLMModels, removeModelFromGroup } = useGateway();
    const columnItems = useSlot('resource-group.model-columns');
    const toast = useToast();

    const [effectiveModels, setEffectiveModels] = useState<EffectiveLLModelsResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [searchTerm, setSearchTerm] = useState('');
    const [viewMode, setViewMode] = useState<ViewMode>('all');
    const [removingId, setRemovingId] = useState<string | null>(null);
    const [deleteError, setDeleteError] = useState<string | null>(null);

    // Governance dialog state
    const [governanceModel, setGovernanceModel] = useState<LLModelInfo | null>(null);

    // Limits dialog state
    const [limitsModel, setLimitsModel] = useState<LLModelInfo | null>(null);

    // Info modal state
    const [showInheritanceInfo, setShowInheritanceInfo] = useState(false);
    const [showAddModelsInfo, setShowAddModelsInfo] = useState(false);

    // Delete confirmation state
    const [deleteConfirmation, setDeleteConfirmation] = useState<{
        modelId: string;
        modelName: string;
    } | null>(null);

    useEffect(() => {
        runBackground(loadModels(), 'models');
    }, [group.id]);

    const loadModels = async () => {
        try {
            setLoading(true);
            setError('');
            const response = await fetchEffectiveLLMModels(group.id);
            setEffectiveModels(response);
        } catch (err) {
            console.error('Failed to load models:', errorKind(err));
            setError(errorMessage(err, 'Failed to load AI models'));
        } finally {
            setLoading(false);
        }
    };

    const handleRemoveModel = (modelId: string, modelName: string) => {
        // Show confirmation modal
        setDeleteError(null);
        setDeleteConfirmation({
            modelId,
            modelName
        });
    };

    const confirmRemoveModel = async () => {
        if (!deleteConfirmation) return;

        const { modelId, modelName } = deleteConfirmation;

        try {
            setRemovingId(modelId);
            setError('');
            await removeModelFromGroup(group.id, modelId);
            await loadModels();
            onUpdate();

            // Show success toast
            toast.success('Model Removed', `${modelName} has been removed from ${formatGroupLabel(group)}.`);

            // Close modal
            setDeleteConfirmation(null);
        } catch (err: any) {
            console.error('Failed to remove model:', errorKind(err));
            setDeleteError(err.detail || err.message || 'Failed to remove model');
        } finally {
            setRemovingId(null);
        }
    };

    // Columns contributed to the `resource-group.model-columns` slot (the
    // portal visibility column is). A cell tells the tab before it writes
    // and after it has.
    const extraColumns: ModelTableColumn[] = columnItems.map((item) => ({
        id: item.id,
        header: item.header,
        headerTitle: item.headerTitle,
        render: (model) => (
            <item.Cell group={group} model={model} onBeforeChange={() => setError('')} onChanged={loadModels} />
        ),
    }));

    // Get models based on view mode
    const getDisplayModels = (): LLModelInfo[] => {
        if (!effectiveModels) return [];

        switch (viewMode) {
            case 'direct':
                return effectiveModels.direct_models;
            case 'inherited':
                return effectiveModels.inherited_models;
            case 'all':
            default:
                return effectiveModels.effective_models;
        }
    };

    // Filter models by search term
    const filteredModels = getDisplayModels().filter(model =>
        model.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
        model.id.toLowerCase().includes(searchTerm.toLowerCase())
    );

    // Resolve the badge shown next to each model. Prefer the model's
    // *actual* `provider` field (the platform that serves it — Ollama,
    // Bedrock, Together, OpenAI…) over any name-based heuristic.
    //
    // Why this order: a Llama-3.3-70B model hosted on Ollama is
    // "Ollama" from a governance / billing / health-check standpoint,
    // not "Meta" — Ollama is who we actually call. The previous
    // version of this function decided purely from the model name and
    // mislabelled every Ollama / Bedrock / Together-hosted Llama as
    // "Meta", which then cascaded into the ProviderLogo lookup
    // falling through to `custom` (the up-and-down-arrows icon)
    // because no "meta" key exists in providerLogos.tsx.
    //
    // The name-heuristic is kept as a fallback for the rare case
    // where `provider` arrives missing or as the literal string
    // "custom" (operator-defined OpenAI-compat endpoints, etc.).
    const PROVIDER_DISPLAY: Record<string, { display: string; color: string; bgColor: string }> = {
        openai:           { display: 'OpenAI',        color: 'text-green-600',  bgColor: 'bg-surface-alt' },
        anthropic:        { display: 'Anthropic',     color: 'text-muted', bgColor: 'bg-surface-alt' },
        gemini:           { display: 'Google Gemini', color: 'text-primary',   bgColor: 'bg-primary-pale' },
        vertex_ai:        { display: 'Vertex AI',     color: 'text-primary',   bgColor: 'bg-primary-pale' },
        azure:            { display: 'Azure OpenAI',  color: 'text-secondary',    bgColor: 'bg-surface-alt' },
        azure_ai:         { display: 'Azure AI',      color: 'text-secondary',    bgColor: 'bg-surface-alt' },
        bedrock:          { display: 'Bedrock',       color: 'text-amber-700',  bgColor: 'bg-surface-alt' },
        bedrock_converse: { display: 'Bedrock',       color: 'text-amber-700',  bgColor: 'bg-surface-alt' },
        sagemaker:        { display: 'SageMaker',     color: 'text-amber-700',  bgColor: 'bg-surface-alt' },
        mistral:          { display: 'Mistral',       color: 'text-muted', bgColor: 'bg-surface-alt' },
        cohere:           { display: 'Cohere',        color: 'text-secondary',bgColor: 'bg-surface-alt' },
        xai:              { display: 'xAI',           color: 'text-default',    bgColor: 'bg-surface-alt' },
        deepseek:         { display: 'DeepSeek',      color: 'text-primary',   bgColor: 'bg-primary-pale' },
        moonshot:         { display: 'Moonshot',      color: 'text-default',    bgColor: 'bg-surface-alt' },
        minimax:          { display: 'MiniMax',       color: 'text-muted',   bgColor: 'bg-surface-alt' },
        groq:             { display: 'Groq',          color: 'text-red-600',    bgColor: 'bg-surface-alt' },
        together_ai:      { display: 'Together AI',   color: 'text-muted', bgColor: 'bg-surface-alt' },
        fireworks_ai:     { display: 'Fireworks',     color: 'text-muted', bgColor: 'bg-surface-alt' },
        perplexity:       { display: 'Perplexity',    color: 'text-secondary',   bgColor: 'bg-surface-alt' },
        openrouter:       { display: 'OpenRouter',    color: 'text-muted', bgColor: 'bg-surface-alt' },
        sambanova:        { display: 'SambaNova',     color: 'text-muted', bgColor: 'bg-surface-alt' },
        cerebras:         { display: 'Cerebras',      color: 'text-primary',   bgColor: 'bg-primary-pale' },
        ai21:             { display: 'AI21',          color: 'text-muted', bgColor: 'bg-surface-alt' },
        deepinfra:        { display: 'DeepInfra',     color: 'text-muted',   bgColor: 'bg-surface-alt' },
        ollama:           { display: 'Ollama',        color: 'text-default',    bgColor: 'bg-surface-alt' },
        vllm:             { display: 'vLLM',          color: 'text-muted',   bgColor: 'bg-surface-alt' },
        nvidia_nim:       { display: 'NVIDIA NIM',    color: 'text-green-700',  bgColor: 'bg-surface-alt' },
        databricks:       { display: 'Databricks',    color: 'text-red-600',    bgColor: 'bg-surface-alt' },
        huggingface:      { display: 'Hugging Face',  color: 'text-yellow-700', bgColor: 'bg-surface-alt' },
        lm_studio:        { display: 'LM Studio',     color: 'text-primary',   bgColor: 'bg-primary-pale' },
        voyage:           { display: 'Voyage AI',     color: 'text-secondary',   bgColor: 'bg-surface-alt' },
        jina_ai:          { display: 'Jina AI',       color: 'text-muted', bgColor: 'bg-surface-alt' },
        elevenlabs:       { display: 'ElevenLabs',    color: 'text-default',    bgColor: 'bg-surface-alt' },
        deepgram:         { display: 'Deepgram',      color: 'text-secondary',bgColor: 'bg-surface-alt' },
        stability_ai:     { display: 'Stability AI',  color: 'text-muted', bgColor: 'bg-surface-alt' },
        fal_ai:           { display: 'Fal AI',        color: 'text-muted',   bgColor: 'bg-surface-alt' },
        routing_group:    { display: 'Routing Group', color: 'text-muted', bgColor: 'bg-surface-alt' },
    };

    const getModelProvider = (modelName: string, providerKey?: string): { provider: string; color: string; bgColor: string } => {
        const key = (providerKey || '').toLowerCase();
        if (key && key !== 'custom' && PROVIDER_DISPLAY[key]) {
            return {
                provider: PROVIDER_DISPLAY[key].display,
                color: PROVIDER_DISPLAY[key].color,
                bgColor: PROVIDER_DISPLAY[key].bgColor,
            };
        }

        // Fallback name-heuristic — only fires when the model has no
        // provider, an unknown one, or the literal `custom` provider.
        const name = modelName.toLowerCase();
        if (name.includes('gpt') || name.includes('openai')) {
            return { provider: 'OpenAI', color: 'text-green-600', bgColor: 'bg-surface-alt' };
        } else if (name.includes('claude')) {
            return { provider: 'Anthropic', color: 'text-muted', bgColor: 'bg-surface-alt' };
        } else if (name.includes('gemini') || name.includes('palm')) {
            return { provider: 'Google', color: 'text-primary', bgColor: 'bg-primary-pale' };
        } else if (name.includes('llama') || name.includes('meta')) {
            return { provider: 'Meta', color: 'text-muted', bgColor: 'bg-surface-alt' };
        } else if (name.includes('mistral')) {
            return { provider: 'Mistral', color: 'text-muted', bgColor: 'bg-surface-alt' };
        }
        return { provider: 'Other', color: 'text-secondary', bgColor: 'bg-surface-alt' };
    };

    return (
        <div className="p-6 space-y-6">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div>
                    <h2 className="text-lg font-semibold text-default">AI Models</h2>
                    <p className="text-sm text-secondary mt-1">
                        Large Language Models (LLMs) available to this resource group.
                    </p>
                </div>
                <div className="flex items-center space-x-2">
                    <button
                        onClick={() => setShowInheritanceInfo(true)}
                        className="flex items-center space-x-2 px-3 py-2 text-sm text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt transition-colors"
                        title="Learn about model inheritance"
                    >
                        <HelpCircle className="w-4 h-4" />
                        <span>Inheritance</span>
                    </button>
                    <button
                        onClick={() => setShowAddModelsInfo(true)}
                        className="flex items-center space-x-2 px-3 py-2 text-sm text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt transition-colors"
                        title="Learn how to add models"
                    >
                        <HelpCircle className="w-4 h-4" />
                        <span>Add Models</span>
                    </button>
                </div>
            </div>

            {/* Error Message */}
            {error && (
                <div className="flex items-start space-x-3 p-4 bg-surface-alt border border-default rounded-lg">
                    <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
                    <div className="flex-1 min-h-0 min-w-0">
                        <p className="text-sm font-medium text-red-900">Error</p>
                        <p className="text-sm text-red-700 mt-1">{error}</p>
                    </div>
                </div>
            )}

            {/* Filters & Search */}
            <div className="flex flex-col sm:flex-row items-stretch sm:items-center space-y-3 sm:space-y-0 sm:space-x-4">
                {/* Search */}
                <div className="flex-1 relative min-h-0 min-w-0">
                    <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted" />
                    <input
                        type="text"
                        value={searchTerm}
                        onChange={(e) => setSearchTerm(e.target.value)}
                        placeholder="Search models..."
                        className="w-full pl-10 pr-10 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                    />
                    {searchTerm && (
                        <button
                            onClick={() => setSearchTerm('')}
                            className="absolute right-3 top-1/2 transform -translate-y-1/2 text-muted hover:text-secondary"
                        >
                            <X className="w-4 h-4" />
                        </button>
                    )}
                </div>

                {/* View Mode Tabs */}
                <div className="flex items-center space-x-1 bg-surface-raised rounded-lg p-1">
                    <button
                        onClick={() => setViewMode('all')}
                        className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                            viewMode === 'all'
                                ? 'bg-surface text-default shadow-sm'
                                : 'text-secondary hover:text-default'
                        }`}
                    >
                        All ({effectiveModels?.effective_count || 0})
                    </button>
                    <button
                        onClick={() => setViewMode('direct')}
                        className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                            viewMode === 'direct'
                                ? 'bg-surface text-default shadow-sm'
                                : 'text-secondary hover:text-default'
                        }`}
                    >
                        Direct ({effectiveModels?.direct_count || 0})
                    </button>
                    <button
                        onClick={() => setViewMode('inherited')}
                        className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                            viewMode === 'inherited'
                                ? 'bg-surface text-default shadow-sm'
                                : 'text-secondary hover:text-default'
                        }`}
                    >
                        Inherited ({effectiveModels?.inherited_count || 0})
                    </button>
                </div>
            </div>

            {/* Info Banner for Inherited View */}
            {viewMode === 'inherited' && effectiveModels && effectiveModels.inherited_count > 0 && (
                <div className="flex items-start space-x-3 p-4 bg-surface-alt border border-default rounded-lg">
                    <Info className="w-5 h-5 text-muted flex-shrink-0 mt-0.5" />
                    <div className="flex-1 min-h-0 min-w-0">
                        <p className="text-sm font-medium text-secondary">Inherited Models</p>
                        <p className="text-sm text-secondary mt-1">
                            These models are inherited from parent groups in the hierarchy. To modify them, edit the parent group.
                        </p>
                    </div>
                </div>
            )}

            {/* Models List */}
            {loading ? (
                <div className="flex items-center justify-center h-48">
                    <div className="text-center">
                        <Loader className="w-8 h-8 text-primary-600 animate-spin mx-auto mb-2" />
                        <p className="text-sm text-secondary">Loading models...</p>
                    </div>
                </div>
            ) : filteredModels.length === 0 ? (
                <div className="text-center py-12 bg-surface-alt rounded-lg border-2 border-dashed border-strong">
                    <Brain className="w-12 h-12 text-muted mx-auto mb-3" />
                    <p className="text-default font-medium mb-1">
                        {searchTerm
                            ? 'No models found matching your search'
                            : viewMode === 'direct'
                                ? 'No direct models assigned yet'
                                : viewMode === 'inherited'
                                    ? 'No inherited models from parent groups'
                                    : 'No AI models available'}
                    </p>
                    {!searchTerm && (
                        <p className="text-sm text-muted mt-2">
                            {viewMode === 'direct'
                                ? 'Click "Add Models" to assign AI models directly to this group'
                                : viewMode === 'inherited'
                                    ? 'Models will appear here when assigned to parent groups'
                                    : 'Add models from the AI Models section or assign them to parent groups'}
                        </p>
                    )}
                </div>
            ) : (
                <GroupLLModelsTableView
                    models={filteredModels}
                    onRemove={handleRemoveModel}
                    onConfigureGovernance={setGovernanceModel}
                    onConfigureLimits={setLimitsModel}
                    extraColumns={extraColumns}
                    removingId={removingId}
                    getModelProvider={getModelProvider}
                />
            )}

            {/* Delete Confirmation Modal */}
            {deleteConfirmation && (
                <GenericDeleteConfirmationModal
                    isOpen={true}
                    onClose={() => {
                        setDeleteConfirmation(null);
                        setDeleteError(null);
                    }}
                    onConfirm={asyncHandler(confirmRemoveModel, 'confirm')}
                    action="remove"
                    itemType="model"
                    itemName={deleteConfirmation.modelName}
                    itemInfo={{
                        id: deleteConfirmation.modelId,
                        description: 'AI Model',
                        enabled: true,
                    }}
                    customWarning={`This will remove ${deleteConfirmation.modelName} from ${formatGroupLabel(group)}. Members will no longer have access to this model.`}
                    customNote="The model will still be available in the system and can be re-added later."
                    isDeleting={removingId === deleteConfirmation.modelId}
                    errorMessage={deleteError}
                />
            )}

            {/* Inheritance Info Modal */}
            {showInheritanceInfo && (
                <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
                    <div className="bg-surface rounded-lg shadow-xl max-w-2xl w-full">
                        <div className="flex items-center justify-between p-6 border-b border-default">
                            <div className="flex items-center space-x-3">
                                <div className="p-2 bg-surface-alt rounded-lg">
                                    <Info className="w-5 h-5 text-muted" />
                                </div>
                                <h2 className="text-xl font-semibold text-default">
                                    Model Inheritance
                                </h2>
                            </div>
                            <button
                                onClick={() => setShowInheritanceInfo(false)}
                                className="text-muted hover:text-secondary transition-colors"
                            >
                                <X className="w-5 h-5" />
                            </button>
                        </div>
                        <div className="p-6 space-y-4">
                            <div>
                                <h3 className="text-base font-semibold text-default mb-2">
                                    What is Model Inheritance?
                                </h3>
                                <p className="text-sm text-secondary">
                                    Resource groups can inherit AI models from their parent groups in the hierarchy.
                                    This means models assigned to parent groups automatically become available to all descendant groups.
                                </p>
                            </div>

                            <div className="bg-surface-alt border border-default rounded-lg p-4">
                                <h4 className="text-sm font-semibold text-secondary mb-2">
                                    How It Works
                                </h4>
                                <ul className="space-y-2 text-sm text-secondary">
                                    <li className="flex items-start space-x-2">
                                        <ArrowUpRight className="w-4 h-4 text-muted flex-shrink-0 mt-0.5" />
                                        <span><strong>Direct Models:</strong> Models explicitly assigned to this group</span>
                                    </li>
                                    <li className="flex items-start space-x-2">
                                        <ArrowUpRight className="w-4 h-4 text-muted flex-shrink-0 mt-0.5" />
                                        <span><strong>Inherited Models:</strong> Models from parent groups (shown with light blue background)</span>
                                    </li>
                                    <li className="flex items-start space-x-2">
                                        <ArrowUpRight className="w-4 h-4 text-muted flex-shrink-0 mt-0.5" />
                                        <span><strong>Effective Models:</strong> All models available to this group (direct + inherited)</span>
                                    </li>
                                </ul>
                            </div>

                            <div>
                                <h4 className="text-sm font-semibold text-default mb-2">
                                    Managing Inherited Models
                                </h4>
                                <p className="text-sm text-secondary">
                                    Inherited models cannot be removed from this group directly. To modify inherited models:
                                </p>
                                <ol className="mt-2 space-y-1 text-sm text-secondary list-decimal list-inside">
                                    <li>Navigate to the parent group that owns the model</li>
                                    <li>Remove or modify the model assignment there</li>
                                    <li>Changes will automatically propagate to all descendant groups</li>
                                </ol>
                            </div>

                            <div className="bg-surface-alt border border-default rounded-lg p-4">
                                <p className="text-xs text-secondary">
                                    <strong>Example:</strong> If "Organization" group has GPT-4, all teams under that organization
                                    (Engineering, Marketing, etc.) will automatically inherit access to GPT-4.
                                </p>
                            </div>
                        </div>
                        <div className="flex items-center justify-end p-6 border-t border-default bg-surface-alt">
                            <button
                                onClick={() => setShowInheritanceInfo(false)}
                                className="px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors"
                            >
                                Got it
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* LLM Governance Dialog */}
            {governanceModel && (
                <LLMGovernanceDialog
                    isOpen={true}
                    onClose={() => setGovernanceModel(null)}
                    onSave={asyncHandler(loadModels, 'save')}
                    groupId={group.id}
                    groupName={formatGroupLabel(group)}
                    model={governanceModel}
                />
            )}

            {/* Per-Model Limits Dialog */}
            {limitsModel && (
                <LLMModelLimitsDialog
                    isOpen={true}
                    onClose={() => setLimitsModel(null)}
                    onSave={asyncHandler(loadModels, 'save')}
                    groupId={group.id}
                    groupName={formatGroupLabel(group)}
                    model={limitsModel}
                />
            )}

            {/* Add Models Info Modal */}
            {showAddModelsInfo && (
                <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
                    <div className="bg-surface rounded-lg shadow-xl max-w-2xl w-full">
                        <div className="flex items-center justify-between p-6 border-b border-default">
                            <div className="flex items-center space-x-3">
                                <div className="p-2 bg-primary-pale rounded-lg">
                                    <HelpCircle className="w-5 h-5 text-primary" />
                                </div>
                                <h2 className="text-xl font-semibold text-default">
                                    How to Add Models to a Resource Group
                                </h2>
                            </div>
                            <button
                                onClick={() => setShowAddModelsInfo(false)}
                                className="text-muted hover:text-secondary transition-colors"
                            >
                                <X className="w-5 h-5" />
                            </button>
                        </div>
                        <div className="p-6 space-y-4">
                            <p className="text-sm text-secondary">
                                Models are not added directly in the Groups interface. Instead, models are managed
                                centrally and then linked to groups through the AI Models configuration page.
                            </p>

                            <div className="bg-primary-pale border border-primary rounded-lg p-4">
                                <h3 className="text-sm font-semibold text-primary mb-3">
                                    Step-by-Step Instructions
                                </h3>
                                <ol className="space-y-3 text-sm text-primary">
                                    <li className="flex items-start space-x-3">
                                        <span className="flex-shrink-0 w-6 h-6 bg-primary text-white rounded-full flex items-center justify-center text-xs font-bold">
                                            1
                                        </span>
                                        <div>
                                            <p className="font-medium">Navigate to AI Model configuration page</p>
                                            <p className="text-primary mt-1">
                                                Go to <strong>Resources | AI Models | Configuration</strong> in the left side navigation
                                            </p>
                                        </div>
                                    </li>
                                    <li className="flex items-start space-x-3">
                                        <span className="flex-shrink-0 w-6 h-6 bg-primary text-white rounded-full flex items-center justify-center text-xs font-bold">
                                            3
                                        </span>
                                        <div>
                                            <p className="font-medium">Find the Model</p>
                                            <p className="text-primary mt-1">
                                                Find the model you want to add and click <strong>Enable</strong>.
                                                Use the <strong>Browse</strong> tree view to find the provider and model name.
                                                Or use the <strong>Search</strong> box to filter models by name.
                                            </p>
                                        </div>
                                    </li>
                                    <li className="flex items-start space-x-3">
                                        <span className="flex-shrink-0 w-6 h-6 bg-primary text-white rounded-full flex items-center justify-center text-xs font-bold">
                                            4
                                        </span>
                                        <div>
                                            <p className="font-medium">Set API Key</p>
                                            <p className="text-primary mt-1">
                                                Click the <strong>Edit</strong> action and then click <strong>API</strong> in left menu to set API Key.
                                            </p>
                                        </div>
                                    </li>
                                    <li className="flex items-start space-x-3">
                                        <span className="flex-shrink-0 w-6 h-6 bg-primary text-white rounded-full flex items-center justify-center text-xs font-bold">
                                            5
                                        </span>
                                        <div>
                                            <p className="font-medium">Assign Groups</p>
                                            <p className="text-primary mt-1">
                                                In the model edit form, click <strong>Access Control</strong> in left menu and select the Resource Groups that should have access to this model.
                                            </p>
                                        </div>
                                    </li>
                                    <li className="flex items-start space-x-3">
                                        <span className="flex-shrink-0 w-6 h-6 bg-primary text-white rounded-full flex items-center justify-center text-xs font-bold">
                                            6
                                        </span>
                                        <div>
                                            <p className="font-medium">Save Changes</p>
                                            <p className="text-primary mt-1">
                                                Click <strong>Save</strong> to apply the API Key and Resource Group assignments.
                                            </p>
                                        </div>
                                    </li>
                                    <li className="flex items-start space-x-3">
                                        <span className="flex-shrink-0 w-6 h-6 bg-primary text-white rounded-full flex items-center justify-center text-xs font-bold">
                                            6
                                        </span>
                                        <div>
                                            <p className="font-medium">Test connection</p>
                                            <p className="text-primary mt-1">
                                                Click the <strong>Test connection</strong> action. Make sure it shows Healthy!
                                            </p>
                                        </div>
                                    </li>
                                </ol>
                            </div>

                            <div className="bg-surface-alt border border-default rounded-lg p-4">
                                <div className="flex items-start space-x-2">
                                    <AlertCircle className="w-4 h-4 text-yellow-600 flex-shrink-0 mt-0.5" />
                                    <div>
                                        <p className="text-sm font-medium text-yellow-900">Important Note</p>
                                        <p className="text-sm text-yellow-800 mt-1">
                                            The model will appear in this group's list after you save the changes.
                                            If the group inherits from parents, the model will also automatically appear in all descendant groups.
                                        </p>
                                    </div>
                                </div>
                            </div>
                        </div>
                        <div className="flex items-center justify-end p-6 border-t border-default bg-surface-alt">
                            <button
                                onClick={() => setShowAddModelsInfo(false)}
                                className="px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors"
                            >
                                Got it
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
};

export default GroupLLModelsTab;
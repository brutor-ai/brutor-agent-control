/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import {
    X, Search, SlidersHorizontal, Plus,
    Sparkles, DollarSign, AlertCircle, Loader2, ChevronDown,
    ChevronRight,
} from 'lucide-react';
import { LLMCatalogEntry } from '../../../lib/llmTypes';
import { useLLMSearch, LLMCatalogSearchParams } from '../../../hooks/useLLMSearch';
import { ProviderLogo } from './providerLogos';
import { getProviderInfo, PROVIDER_INFO } from './providerInfo';
import { asyncHandler } from '../../../lib/asyncSafe';
import { errorKind } from '../../../lib/narrow';

interface LLMSearchModalProps {
    isOpen: boolean;
    onClose: () => void;
    /**
     * Import the chosen catalog entry as a configured model instance.
     * Resolves once the import round-trip completes so the modal can
     * clear its per-row spinner.
     */
    onImportModel: (entry: LLMCatalogEntry) => Promise<void>;
}

const LLMSearchModal: React.FC<LLMSearchModalProps> = ({
    isOpen,
    onClose,
    onImportModel,
}) => {
    const { search, loading, error, searchResults } = useLLMSearch();

    // Search form state
    const [searchQuery, setSearchQuery] = useState('');
    const [selectedProvider, setSelectedProvider] = useState<string>('');
    const [showAdvanced, setShowAdvanced] = useState(false);

    // Advanced filters
    const [recommendedOnly, setRecommendedOnly] = useState(false);
    // Mode filter — empty string means "any mode". Values must match
    // the catalog mode vocabulary on the gateway side; the dropdown
    // below is the single source of truth for what the operator can pick.
    const [mode, setMode] = useState<string>('');

    // Per-row import-in-flight set.
    const [importingIds, setImportingIds] = useState<Set<string>>(new Set());

    // Provider list for dropdown — derived from the icon registry so
    // adding a new provider in `providerLogos.tsx` automatically
    // populates this filter. Excludes `routing_group` (synthetic, not a
    // real LLM provider) and `custom` (fallback bucket).
    const providers = Object.keys(PROVIDER_INFO)
        .filter(k => k !== 'routing_group' && k !== 'custom')
        .sort((a, b) =>
            getProviderInfo(a).displayName.localeCompare(getProviderInfo(b).displayName),
        );

    // Reset filters when modal closes
    useEffect(() => {
        if (!isOpen) {
            setSearchQuery('');
            setSelectedProvider('');
            setRecommendedOnly(false);
            setMode('');
            setShowAdvanced(false);
        }
    }, [isOpen]);

    // Perform search
    const handleSearch = async () => {
        const params: LLMCatalogSearchParams = {
            offset: 0,
            limit: 100,
        };

        if (searchQuery.trim()) {
            params.q = searchQuery.trim();
        }
        if (selectedProvider) {
            params.provider_key = selectedProvider;
        }
        if (mode) {
            params.mode = mode;
        }
        if (recommendedOnly) {
            params.recommended = true;
        }

        await search(params);
    };

    // Import a catalog entry
    const handleImport = async (entry: LLMCatalogEntry) => {
        setImportingIds(prev => new Set(prev).add(entry.id));
        try {
            await onImportModel(entry);
        } catch (err) {
            console.error('Failed to import model:', errorKind(err));
        } finally {
            setImportingIds(prev => {
                const next = new Set(prev);
                next.delete(entry.id);
                return next;
            });
        }
    };

    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-50">
            <div className="bg-surface rounded-lg shadow-xl w-full max-w-5xl max-h-[90vh] flex flex-col">
                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b">
                    <h2 className="text-xl font-semibold text-default">
                        Browse Model Catalog
                    </h2>
                    <button
                        onClick={onClose}
                        className="text-muted hover:text-secondary transition-colors"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                {/* Search Form */}
                <div className="px-6 py-4 border-b bg-surface-alt">
                    <div className="space-y-3">
                        {/* Basic Search */}
                        <div className="flex space-x-2">
                            <div className="flex-1 relative min-h-0 min-w-0">
                                <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted" />
                                <input
                                    type="text"
                                    value={searchQuery}
                                    onChange={(e) => setSearchQuery(e.target.value)}
                                    onKeyPress={asyncHandler((e) => e.key === 'Enter' && handleSearch(), 'key press')}
                                    placeholder="Search by name or model ID..."
                                    className="w-full pl-10 pr-4 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                                />
                            </div>

                            <select
                                value={selectedProvider}
                                onChange={(e) => setSelectedProvider(e.target.value)}
                                className="px-4 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                            >
                                <option value="">All Providers</option>
                                {providers.map(provider => (
                                    <option key={provider} value={provider}>
                                        {getProviderInfo(provider).displayName}
                                    </option>
                                ))}
                            </select>

                            <button
                                onClick={asyncHandler(handleSearch, 'click')}
                                disabled={loading}
                                className="px-6 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 disabled:opacity-50 disabled:cursor-not-allowed flex items-center space-x-2"
                            >
                                {loading ? (
                                    <>
                                        <Loader2 className="w-4 h-4 animate-spin" />
                                        <span>Searching...</span>
                                    </>
                                ) : (
                                    <>
                                        <Search className="w-4 h-4" />
                                        <span>Search</span>
                                    </>
                                )}
                            </button>
                        </div>

                        {/* Advanced Filters Toggle */}
                        <div>
                            <button
                                onClick={() => setShowAdvanced(!showAdvanced)}
                                className="flex items-center space-x-2 text-sm text-secondary hover:text-default"
                            >
                                {showAdvanced ? (
                                    <ChevronDown className="w-4 h-4" />
                                ) : (
                                    <ChevronRight className="w-4 h-4" />
                                )}
                                <SlidersHorizontal className="w-4 h-4" />
                                <span>Advanced Filters</span>
                            </button>
                        </div>

                        {/* Advanced Filters */}
                        {showAdvanced && (
                            <div className="grid grid-cols-2 gap-4 pt-3 border-t">
                                <div className="space-y-2">
                                    <label className="block text-sm font-medium text-secondary">
                                        Mode
                                    </label>
                                    <select
                                        value={mode}
                                        onChange={(e) => setMode(e.target.value)}
                                        className="w-full px-3 py-1.5 border border-strong rounded text-sm focus:ring-2 focus:ring-primary-500 focus:border-transparent bg-surface"
                                    >
                                        <option value="">All modes</option>
                                        <option value="chat">Chat</option>
                                        <option value="responses">Responses</option>
                                        <option value="completion">Completion</option>
                                        <option value="embedding">Embedding</option>
                                        <option value="image_generation">Image generation</option>
                                        <option value="audio_speech">Audio · TTS</option>
                                        <option value="audio_transcription">Audio · STT</option>
                                        <option value="video_generation">Video generation</option>
                                        <option value="rerank">Rerank</option>
                                        <option value="moderation">Moderation</option>
                                    </select>
                                </div>

                                <div className="space-y-2">
                                    <label className="block text-sm font-medium text-secondary">
                                        Status
                                    </label>
                                    <label className="flex items-center space-x-2 pt-1.5">
                                        <input
                                            type="checkbox"
                                            checked={recommendedOnly}
                                            onChange={(e) => setRecommendedOnly(e.target.checked)}
                                            className="rounded border-strong text-primary-600 focus:ring-primary-500"
                                        />
                                        <span className="text-sm text-secondary">Recommended only</span>
                                    </label>
                                </div>
                            </div>
                        )}
                    </div>
                </div>

                {/* Results */}
                <div className="flex-1 overflow-auto px-6 py-4 min-h-0 min-w-0">
                    {error && (
                        <div className="mb-4 p-4 bg-red-50 border border-red-200 rounded-lg flex items-start space-x-2">
                            <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
                            <div className="flex-1 min-h-0 min-w-0">
                                <p className="text-sm font-medium text-red-900">Search Failed</p>
                                <p className="text-sm text-red-700 mt-1">{error}</p>
                            </div>
                        </div>
                    )}

                    {!searchResults && !loading && (
                        <div className="text-center py-12 text-muted">
                            <Search className="w-12 h-12 mx-auto mb-4 text-muted" />
                            <p className="text-lg font-medium">Browse the Model Catalog</p>
                            <p className="text-sm mt-2">Enter search criteria and click Search to find models to add</p>
                        </div>
                    )}

                    {searchResults && searchResults.catalog.length === 0 && (
                        <div className="text-center py-12 text-muted">
                            <Search className="w-12 h-12 mx-auto mb-4 text-muted" />
                            <p className="text-lg font-medium">No Models Found</p>
                            <p className="text-sm mt-2">Try adjusting your search criteria</p>
                        </div>
                    )}

                    {searchResults && searchResults.catalog.length > 0 && (
                        <div className="space-y-2">
                            {/* Results Header */}
                            <div className="flex items-center justify-between mb-4">
                                <p className="text-sm text-secondary">
                                    Found <span className="font-semibold text-default">{searchResults.total}</span> model(s)
                                </p>
                            </div>

                            {/* Catalog Entry Cards */}
                            {searchResults.catalog.map((entry) => (
                                <CatalogSearchResult
                                    key={entry.id}
                                    entry={entry}
                                    onImport={asyncHandler(() => handleImport(entry), 'import')}
                                    isImporting={importingIds.has(entry.id)}
                                />
                            ))}
                        </div>
                    )}
                </div>

                {/* Footer */}
                <div className="px-6 py-4 border-t bg-surface-alt flex justify-end">
                    <button
                        onClick={onClose}
                        className="px-4 py-2 text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt"
                    >
                        Close
                    </button>
                </div>
            </div>
        </div>
    );
};

// Catalog Entry Search Result Card Component
interface CatalogSearchResultProps {
    entry: LLMCatalogEntry;
    onImport: () => void;
    isImporting: boolean;
}

const CatalogSearchResult: React.FC<CatalogSearchResultProps> = ({
    entry,
    onImport,
    isImporting,
}) => {
    const formatCost = (cost: number | null | undefined) => {
        if (cost == null) return 'N/A';
        if (cost === 0) return 'Free';
        return `$${cost.toFixed(2)}`;
    };

    return (
        <div className="border rounded-lg p-4 bg-surface border-default">
            <div className="flex items-start justify-between">
                {/* Left: Model Info */}
                <div className="flex-1 min-w-0 min-h-0">
                    <div className="flex items-center space-x-2 mb-2">
                        {entry.icon_url ? (
                            <img
                                src={entry.icon_url}
                                alt={getProviderInfo(entry.provider_key).displayName}
                                className="w-5 h-5 rounded object-contain flex-shrink-0"
                            />
                        ) : (
                            <ProviderLogo provider={entry.provider_key} size="sm" />
                        )}
                        <h3 className="text-base font-semibold text-default truncate">
                            {entry.display_name}
                        </h3>
                        {entry.recommended && (
                            <span className="flex items-center space-x-1 px-2 py-0.5 bg-amber-100 text-amber-700 text-xs rounded">
                                <Sparkles className="w-3 h-3" />
                                <span>Recommended</span>
                            </span>
                        )}
                        <span className="px-2 py-0.5 bg-surface-raised text-secondary text-xs rounded">
                            {entry.mode}
                        </span>
                        {entry.requires_api_url && (
                            <span
                                className="px-2 py-0.5 bg-blue-100 text-blue-700 text-xs rounded"
                                title="The gateway knows no address for this model: adding it asks for the API URL of your own endpoint."
                            >
                                Needs API URL
                            </span>
                        )}
                    </div>

                    <p className="text-sm text-secondary mb-2">
                        <code className="bg-surface-raised px-1.5 py-0.5 rounded text-xs">
                            {entry.model_name}
                        </code>
                    </p>

                    {entry.description && (
                        <p className="text-sm text-secondary mb-3">
                            {entry.description}
                        </p>
                    )}

                    {/* Cost */}
                    {(entry.default_cost_per_input_token != null || entry.default_cost_per_output_token != null) && (
                        <div className="flex items-center flex-wrap gap-3 text-sm text-secondary">
                            <div className="flex items-center space-x-1">
                                <DollarSign className="w-4 h-4" />
                                <span>
                                    {formatCost(entry.default_cost_per_input_token)}/
                                    {formatCost(entry.default_cost_per_output_token)}
                                </span>
                                <span className="text-xs text-muted">per 1M tokens</span>
                            </div>
                        </div>
                    )}
                </div>

                {/* Right: Add Button */}
                <button
                    onClick={onImport}
                    disabled={isImporting}
                    className="ml-4 px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 disabled:opacity-50 disabled:cursor-not-allowed flex items-center space-x-2 flex-shrink-0"
                >
                    {isImporting ? (
                        <>
                            <Loader2 className="w-4 h-4 animate-spin" />
                            <span>Adding...</span>
                        </>
                    ) : (
                        <>
                            <Plus className="w-4 h-4" />
                            <span>Add</span>
                        </>
                    )}
                </button>
            </div>
        </div>
    );
};

export default LLMSearchModal;

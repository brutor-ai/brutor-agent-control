/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useMemo } from 'react';
import { ChevronDown, ChevronRight, Search, Plus, Check, Loader2, Sparkles } from 'lucide-react';
import { LLMCatalogEntry } from '../../../lib/llmTypes';
import { ProviderLogo } from './providerLogos';
import { getProviderInfo, type ProviderInfo } from './providerInfo';
import { asyncHandler } from '../../../lib/asyncSafe';
import { errorKind } from '../../../lib/narrow';

/**
 * Browse the GLOBAL model catalog (llm_catalog) grouped by provider and
 * import entries as configured instances. This is the read side of the
 * catalog → import → llm_model split: it never mutates instances itself,
 * it delegates each "Add" to `onImport` (which materialises an llm_model
 * row in the parent). Already-configured entries (matched by
 * provider_key:model_name) render as "Added" rather than importable.
 */
interface LLMCatalogTreeProps {
    entries: LLMCatalogEntry[];
    /** `${provider_key}:${model_name}` of models already configured. */
    configuredKeys: Set<string>;
    onImport: (entry: LLMCatalogEntry) => Promise<void>;
    loading?: boolean;
}

interface ProviderGroup {
    provider: string;
    displayName: string;
    entries: LLMCatalogEntry[];
    providerInfo: ProviderInfo;
}

const entryKey = (e: LLMCatalogEntry) => `${e.provider_key}:${e.model_name}`;

const LLMCatalogTree: React.FC<LLMCatalogTreeProps> = ({
    entries,
    configuredKeys,
    onImport,
    loading = false,
}) => {
    const [filter, setFilter] = useState('');
    const [expandedProviders, setExpandedProviders] = useState<Set<string>>(new Set());
    const [importingIds, setImportingIds] = useState<Set<string>>(new Set());

    // Filter then group by provider.
    const providerGroups = useMemo((): ProviderGroup[] => {
        const needle = filter.trim().toLowerCase();
        const groups = new Map<string, ProviderGroup>();

        entries.forEach(entry => {
            if (needle) {
                const hay = `${entry.display_name} ${entry.model_name}`.toLowerCase();
                if (!hay.includes(needle)) return;
            }
            const provider = entry.provider_key || 'custom';
            if (!groups.has(provider)) {
                const providerInfo = getProviderInfo(provider);
                groups.set(provider, {
                    provider,
                    displayName: providerInfo.displayName,
                    entries: [],
                    providerInfo,
                });
            }
            groups.get(provider)!.entries.push(entry);
        });

        return Array.from(groups.values()).sort((a, b) =>
            a.displayName.localeCompare(b.displayName),
        );
    }, [entries, filter]);

    // When filtering, auto-expand the matching groups so results are visible.
    const isFiltering = filter.trim().length > 0;

    const toggleProvider = (provider: string) => {
        setExpandedProviders(prev => {
            const next = new Set(prev);
            next.has(provider) ? next.delete(provider) : next.add(provider);
            return next;
        });
    };
    const expandAll = () => setExpandedProviders(new Set(providerGroups.map(g => g.provider)));
    const collapseAll = () => setExpandedProviders(new Set());

    const handleImport = async (entry: LLMCatalogEntry) => {
        setImportingIds(prev => new Set(prev).add(entry.id));
        try {
            await onImport(entry);
        } catch (err) {
            console.error('Failed to import catalog model:', errorKind(err));
        } finally {
            setImportingIds(prev => {
                const next = new Set(prev);
                next.delete(entry.id);
                return next;
            });
        }
    };

    if (loading) {
        return (
            <div className="flex items-center justify-center py-16">
                <Loader2 className="w-8 h-8 text-primary-600 animate-spin" />
            </div>
        );
    }

    return (
        <div className="py-2">
            {/* Filter + expand/collapse */}
            <div className="px-4 pb-2 space-y-2">
                <div className="relative">
                    <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted" />
                    <input
                        type="text"
                        value={filter}
                        onChange={(e) => setFilter(e.target.value)}
                        placeholder="Filter the catalog by name or model id…"
                        className="w-full pl-10 pr-4 py-2 border border-strong rounded-lg bg-surface text-default focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                    />
                </div>
                <div className="flex items-center justify-end space-x-2 text-xs">
                    <button onClick={expandAll} className="text-primary-600 hover:text-primary-700 font-medium">Expand All</button>
                    <span className="text-muted">•</span>
                    <button onClick={collapseAll} className="text-secondary hover:text-default font-medium">Collapse All</button>
                </div>
            </div>

            {providerGroups.length === 0 ? (
                <div className="p-8 text-center text-muted">
                    <p>No catalog models match.</p>
                </div>
            ) : (
                <div className="space-y-1">
                    {providerGroups.map(group => {
                        const expanded = isFiltering || expandedProviders.has(group.provider);
                        const sorted = [...group.entries].sort((a, b) => a.display_name.localeCompare(b.display_name));
                        return (
                            <div key={group.provider} className="border-b border-default last:border-b-0">
                                <button
                                    onClick={() => toggleProvider(group.provider)}
                                    className="w-full px-4 py-2.5 flex items-center justify-between hover:bg-surface-alt transition-colors"
                                >
                                    <div className="flex items-center space-x-2 min-w-0">
                                        {expanded ? (
                                            <ChevronDown className="w-4 h-4 text-muted flex-shrink-0" />
                                        ) : (
                                            <ChevronRight className="w-4 h-4 text-muted flex-shrink-0" />
                                        )}
                                        <ProviderLogo provider={group.provider} size="sm" />
                                        <span className="text-sm font-medium text-default">{group.displayName}</span>
                                        <span className="text-xs text-muted">{group.entries.length}</span>
                                    </div>
                                </button>

                                {expanded && (
                                    <div className="bg-surface-alt">
                                        {sorted.map(entry => {
                                            const added = configuredKeys.has(entryKey(entry));
                                            const importing = importingIds.has(entry.id);
                                            return (
                                                <div key={entry.id} className="flex items-center justify-between px-4 py-2 pl-10 border-t border-default first:border-t-0">
                                                    <div className="min-w-0 flex-1 min-h-0">
                                                        <div className="flex items-center space-x-2">
                                                            <span className="text-sm font-medium text-default truncate">{entry.display_name}</span>
                                                            {entry.recommended && (
                                                                <span className="flex items-center space-x-1 px-1.5 py-0.5 bg-amber-100 text-amber-700 text-[10px] rounded">
                                                                    <Sparkles className="w-3 h-3" />
                                                                    <span>Recommended</span>
                                                                </span>
                                                            )}
                                                            <span className="px-1.5 py-0.5 bg-surface-raised text-secondary text-[10px] rounded">{entry.mode}</span>
                                                            {entry.requires_api_url && (
                                                                <span
                                                                    className="px-1.5 py-0.5 bg-blue-100 text-blue-700 text-[10px] rounded"
                                                                    title="The gateway knows no address for this model: adding it asks for the API URL of your own endpoint."
                                                                >
                                                                    Needs API URL
                                                                </span>
                                                            )}
                                                        </div>
                                                        <code className="text-xs text-muted">{entry.model_name}</code>
                                                    </div>
                                                    {added ? (
                                                        <span className="ml-3 flex items-center space-x-1 text-xs text-green-600 flex-shrink-0">
                                                            <Check className="w-4 h-4" />
                                                            <span>Added</span>
                                                        </span>
                                                    ) : (
                                                        <button
                                                            onClick={asyncHandler(() => handleImport(entry), 'click')}
                                                            disabled={importing}
                                                            className="ml-3 px-3 py-1.5 bg-primary-600 text-white rounded-lg hover:bg-primary-700 disabled:opacity-50 disabled:cursor-not-allowed flex items-center space-x-1.5 flex-shrink-0 text-sm"
                                                        >
                                                            {importing ? (
                                                                <>
                                                                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                                                    <span>Adding…</span>
                                                                </>
                                                            ) : (
                                                                <>
                                                                    <Plus className="w-3.5 h-3.5" />
                                                                    <span>Add</span>
                                                                </>
                                                            )}
                                                        </button>
                                                    )}
                                                </div>
                                            );
                                        })}
                                    </div>
                                )}
                            </div>
                        );
                    })}
                </div>
            )}
        </div>
    );
};

export default LLMCatalogTree;

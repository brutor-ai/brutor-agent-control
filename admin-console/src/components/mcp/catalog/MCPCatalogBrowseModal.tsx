/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useCallback } from 'react';
import {
    X,
    Search,
    Loader,
    ChevronLeft,
    ChevronRight,
    Package,
    Filter
} from 'lucide-react';
import { useGateway } from '../../../hooks/useGateway';
import {
    MCPCatalogEntry,
    MCPCatalogEntryBrief,
    CatalogEntryListResponse,
    CatalogCategoriesResponse,
    CategoryCount
} from '../../../lib/mcpCatalogTypes';
import MCPCatalogEntryCard from './MCPCatalogEntryCard';
import MCPCatalogEntryDetail from './MCPCatalogEntryDetail';
import { errorKind, errorMessage, failureSentence } from '../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import toast from 'react-hot-toast';
import { useReadFailures } from '../../../hooks/useRead';
import ReadFailureNotice from '../../common/ReadFailureNotice';

interface MCPCatalogBrowseModalProps {
    isOpen: boolean;
    onClose: () => void;
    onImport: (entry: MCPCatalogEntry) => void;
}

const PAGE_SIZE = 12;

const MCPCatalogBrowseModal: React.FC<MCPCatalogBrowseModalProps> = ({ isOpen, onClose, onImport }) => {
    const { gatewayClient } = useGateway();

    // Filters
    const [searchTerm, setSearchTerm] = useState('');
    const [selectedCategory, setSelectedCategory] = useState<string | null>(null);
    const [runtimeFilter, setRuntimeFilter] = useState<'all' | 'remote' | 'containerized'>('all');
    const [authFilter, setAuthFilter] = useState<'all' | 'oauth' | 'api_key' | 'header' | 'none'>('all');

    // Data
    const [entries, setEntries] = useState<MCPCatalogEntryBrief[]>([]);
    const [categories, setCategories] = useState<CategoryCount[]>([]);
    const [readFailure, reads] = useReadFailures<'categories'>();
    const [total, setTotal] = useState(0);
    const [offset, setOffset] = useState(0);
    const [loading, setLoading] = useState(false);
    const [loadingDetail, setLoadingDetail] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // Detail view
    const [selectedEntry, setSelectedEntry] = useState<MCPCatalogEntry | null>(null);

    const totalPages = Math.ceil(total / PAGE_SIZE);
    const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

    const loadCategories = useCallback(async () => {
        try {
            const data = (await gatewayClient.fetchMCPCatalogCategories()) as CatalogCategoriesResponse;
            setCategories(data.categories || []);
            reads.loaded('categories');
        } catch (err) {
            reads.failed('categories', 'Failed to load the categories', err);
        }
    }, [gatewayClient, reads]);

    const loadEntries = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const params: NonNullable<Parameters<typeof gatewayClient.fetchMCPCatalogEntries>[0]> = {
                offset,
                limit: PAGE_SIZE,
                enabled_only: true,
            };
            if (searchTerm) params.search = searchTerm;
            if (selectedCategory) params.category = selectedCategory;
            if (runtimeFilter !== 'all') params.runtime = runtimeFilter;
            if (authFilter !== 'all') params.auth_pattern = authFilter;

            const data = (await gatewayClient.fetchMCPCatalogEntries(params)) as CatalogEntryListResponse;
            setEntries(data.entries || []);
            setTotal(data.total || 0);
        } catch (err) {
            console.error('Failed to load catalog entries:', errorKind(err));
            setError(errorMessage(err, 'Failed to load catalog entries'));
        } finally {
            setLoading(false);
        }
    }, [gatewayClient, offset, searchTerm, selectedCategory, runtimeFilter, authFilter]);

    // Reset offset when filters change
    useEffect(() => {
        setOffset(0);
        setSelectedEntry(null);
    }, [searchTerm, selectedCategory, runtimeFilter, authFilter]);

    // Load data when modal opens or params change
    useEffect(() => {
        if (isOpen) {
            runBackground(loadEntries(), 'entries');
        }
    }, [isOpen, loadEntries]);

    useEffect(() => {
        if (isOpen) {
            runBackground(loadCategories(), 'categories');
        }
    }, [isOpen, loadCategories]);

    const handleCardClick = async (brief: MCPCatalogEntryBrief) => {
        setLoadingDetail(true);
        try {
            const full = (await gatewayClient.fetchMCPCatalogEntry(brief.id)) as MCPCatalogEntry;
            setSelectedEntry(full);
        } catch (err) {
            console.error('Failed to load entry detail:', errorKind(err));
            toast.error(failureSentence('The catalog entry could not be opened', err));
        } finally {
            setLoadingDetail(false);
        }
    };

    const handlePageChange = (newPage: number) => {
        setOffset((newPage - 1) * PAGE_SIZE);
    };

    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4">
            <div className="bg-surface rounded-lg shadow-xl w-full max-w-6xl h-[85vh] flex flex-col">
                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-strong">
                    <div className="flex items-center space-x-3">
                        <Package className="w-5 h-5 text-primary-500" />
                        <h2 className="text-lg font-semibold text-default">MCP Server Catalog</h2>
                        <span className="text-sm text-secondary">({total} entries)</span>
                    </div>
                    <button
                        onClick={onClose}
                        className="p-2 hover:bg-surface-alt rounded-md transition-colors"
                    >
                        <X className="w-5 h-5 text-secondary" />
                    </button>
                </div>

                <div className="flex flex-1 overflow-hidden min-h-0 min-w-0">
                    {/* Left Sidebar */}
                    <div className="w-56 border-r border-strong flex flex-col overflow-y-auto p-4">
                        {/* Runtime Filter */}
                        <div className="mb-4">
                            <h3 className="text-xs font-semibold text-muted uppercase tracking-wider mb-2">Runtime</h3>
                            {(['all', 'remote', 'containerized'] as const).map((opt) => (
                                <button
                                    key={opt}
                                    onClick={() => setRuntimeFilter(opt)}
                                    className={`block w-full text-left px-3 py-1.5 text-sm rounded transition-colors mb-0.5 ${
                                        runtimeFilter === opt
                                            ? 'bg-primary-100 text-primary-700 font-medium'
                                            : 'text-secondary hover:bg-surface-alt'
                                    }`}
                                >
                                    {opt === 'all' ? 'All Runtimes' : opt.charAt(0).toUpperCase() + opt.slice(1)}
                                </button>
                            ))}
                        </div>

                        {/* Auth Filter */}
                        <div className="mb-4">
                            <h3 className="text-xs font-semibold text-muted uppercase tracking-wider mb-2">Auth Pattern</h3>
                            {(['all', 'oauth', 'api_key', 'header', 'none'] as const).map((opt) => {
                                const labels: Record<string, string> = {
                                    all: 'All Auth',
                                    oauth: 'OAuth',
                                    api_key: 'API Key',
                                    header: 'Header',
                                    none: 'None',
                                };
                                return (
                                    <button
                                        key={opt}
                                        onClick={() => setAuthFilter(opt)}
                                        className={`block w-full text-left px-3 py-1.5 text-sm rounded transition-colors mb-0.5 ${
                                            authFilter === opt
                                                ? 'bg-primary-100 text-primary-700 font-medium'
                                                : 'text-secondary hover:bg-surface-alt'
                                        }`}
                                    >
                                        {labels[opt]}
                                    </button>
                                );
                            })}
                        </div>

                        {/* Categories */}
                        <div>
                            <h3 className="text-xs font-semibold text-muted uppercase tracking-wider mb-2">Categories</h3>
                            <button
                                onClick={() => setSelectedCategory(null)}
                                className={`block w-full text-left px-3 py-1.5 text-sm rounded transition-colors mb-0.5 ${
                                    selectedCategory === null
                                        ? 'bg-primary-100 text-primary-700 font-medium'
                                        : 'text-secondary hover:bg-surface-alt'
                                }`}
                            >
                                All Categories
                            </button>
                            <ReadFailureNotice compact className="px-2 py-1" failure={readFailure('categories')} onRetry={() => runBackground(loadCategories(), 'categories')} />
                            {categories.map((cat) => (
                                <button
                                    key={cat.name}
                                    onClick={() => setSelectedCategory(cat.name)}
                                    className={`block w-full text-left px-3 py-1.5 text-sm rounded transition-colors mb-0.5 ${
                                        selectedCategory === cat.name
                                            ? 'bg-primary-100 text-primary-700 font-medium'
                                            : 'text-secondary hover:bg-surface-alt'
                                    }`}
                                >
                                    {cat.name}
                                    <span className="text-xs text-muted ml-1">({cat.count})</span>
                                </button>
                            ))}
                        </div>
                    </div>

                    {/* Main Content */}
                    <div className="flex-1 flex flex-col overflow-hidden min-h-0 min-w-0">
                        {/* Search Bar */}
                        <div className="px-4 py-3 border-b border-strong">
                            <div className="relative">
                                <Search className="w-4 h-4 absolute left-3 top-1/2 transform -translate-y-1/2 text-muted" />
                                <input
                                    type="text"
                                    placeholder="Search catalog entries..."
                                    value={searchTerm}
                                    onChange={(e) => setSearchTerm(e.target.value)}
                                    className="w-full pl-9 pr-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                                />
                            </div>
                        </div>

                        {/* Content Area */}
                        <div className="scroll-region p-4">
                            {selectedEntry ? (
                                /* Detail View */
                                <MCPCatalogEntryDetail
                                    entry={selectedEntry}
                                    onImport={onImport}
                                    onBack={() => setSelectedEntry(null)}
                                />
                            ) : loading || loadingDetail ? (
                                <div className="flex items-center justify-center h-64">
                                    <div className="text-center">
                                        <Loader className="w-8 h-8 animate-spin text-primary-500 mx-auto mb-3" />
                                        <p className="text-sm text-secondary">Loading catalog...</p>
                                    </div>
                                </div>
                            ) : error ? (
                                <div className="flex items-center justify-center h-64">
                                    <div className="text-center">
                                        <p className="text-sm text-red-600 mb-2">{error}</p>
                                        <button
                                            onClick={asyncHandler(loadEntries, 'click')}
                                            className="text-sm text-primary-600 hover:text-primary-700"
                                        >
                                            Try again
                                        </button>
                                    </div>
                                </div>
                            ) : entries.length === 0 ? (
                                <div className="flex items-center justify-center h-64">
                                    <div className="text-center">
                                        <Filter className="w-10 h-10 text-muted mx-auto mb-3" />
                                        <h3 className="text-sm font-medium text-default mb-1">No entries found</h3>
                                        <p className="text-xs text-secondary">Try adjusting your search or filters.</p>
                                    </div>
                                </div>
                            ) : (
                                /* Grid of cards */
                                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                                    {entries.map((entry) => (
                                        <MCPCatalogEntryCard
                                            key={entry.id}
                                            entry={entry}
                                            onClick={asyncHandler(handleCardClick, 'click')}
                                        />
                                    ))}
                                </div>
                            )}
                        </div>

                        {/* Pagination */}
                        {!selectedEntry && totalPages > 1 && (
                            <div className="px-4 py-3 border-t border-strong flex items-center justify-between">
                                <span className="text-sm text-secondary">
                                    Showing {offset + 1}-{Math.min(offset + PAGE_SIZE, total)} of {total}
                                </span>
                                <div className="flex items-center space-x-2">
                                    <button
                                        onClick={() => handlePageChange(currentPage - 1)}
                                        disabled={currentPage <= 1}
                                        className="p-1.5 border border-strong rounded hover:bg-surface-alt disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                                    >
                                        <ChevronLeft className="w-4 h-4" />
                                    </button>
                                    <span className="text-sm text-default">
                                        Page {currentPage} of {totalPages}
                                    </span>
                                    <button
                                        onClick={() => handlePageChange(currentPage + 1)}
                                        disabled={currentPage >= totalPages}
                                        className="p-1.5 border border-strong rounded hover:bg-surface-alt disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                                    >
                                        <ChevronRight className="w-4 h-4" />
                                    </button>
                                </div>
                            </div>
                        )}
                    </div>
                </div>

                {/* Attribution */}
                <div className="px-6 py-2 border-t border-strong text-xs text-muted text-center">
                    Server configurations adapted from the{' '}
                    <a href="https://github.com/obot-platform/tools" target="_blank" rel="noopener noreferrer" className="text-primary-500 hover:text-primary-600">
                        Obot MCP Catalog
                    </a>
                    {' '}(Apache 2.0)
                </div>
            </div>
        </div>
    );
};

export default MCPCatalogBrowseModal;

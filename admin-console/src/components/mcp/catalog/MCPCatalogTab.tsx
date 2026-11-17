/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useCallback } from 'react';
import { TABLE_TH_TYPE } from '../../ui/tableStyles';
import {
    Plus,
    Search,
    RefreshCw,
    Edit,
    Trash2,
    Download,
    Package,
    BookOpen,
    CheckCircle,
    XCircle
} from 'lucide-react';
import { useGateway } from '../../../hooks/useGateway';
import { useToast } from '../../common/Toast';
import {
    MCPCatalogEntry,
    MCPCatalogEntryBrief,
    CatalogEntryListResponse,
    type CatalogEntryCreateRequest,
    type CatalogEntryUpdateRequest,
} from '../../../lib/mcpCatalogTypes';
import MCPCatalogEntryForm from './MCPCatalogEntryForm';
import MCPCatalogBrowseModal from './MCPCatalogBrowseModal';
import MCPCatalogImportModal from './MCPCatalogImportModal';
import GenericDeleteConfirmationModal from '../../common/GenericDeleteConfirmationModal';
import { errorKind, errorMessage } from '../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';

const MCPCatalogTab: React.FC = () => {
    const toast = useToast();
    const { gatewayClient } = useGateway();

    // Data
    const [entries, setEntries] = useState<MCPCatalogEntryBrief[]>([]);
    const [total, setTotal] = useState(0);
    const [loading, setLoading] = useState(true);
    const [loadedOnce, setLoadedOnce] = useState(false);

    // Filters
    const [searchTerm, setSearchTerm] = useState('');
    const [filterRuntime, setFilterRuntime] = useState<'all' | 'remote' | 'containerized'>('all');
    const [filterSource, setFilterSource] = useState<'all' | 'system' | 'custom'>('all');
    const [filterAuth, setFilterAuth] = useState<'all' | 'oauth' | 'api_key' | 'header' | 'none'>('all');

    // Modals
    const [showForm, setShowForm] = useState(false);
    const [editingEntry, setEditingEntry] = useState<MCPCatalogEntry | null>(null);
    const [showBrowseModal, setShowBrowseModal] = useState(false);
    const [importEntry, setImportEntry] = useState<MCPCatalogEntry | null>(null);

    // Delete
    const [deleteConfirmation, setDeleteConfirmation] = useState<{
        entryId: string;
        entryName: string;
        entryInfo: any;
    } | null>(null);
    const [isDeleting, setIsDeleting] = useState(false);
    const [deleteError, setDeleteError] = useState<string | null>(null);

    const loadEntries = useCallback(async () => {
        setLoading(true);
        try {
            const params: Record<string, any> = {};
            if (searchTerm) params.search = searchTerm;
            if (filterRuntime !== 'all') params.runtime = filterRuntime;
            if (filterSource !== 'all') params.source = filterSource;
            if (filterAuth !== 'all') params.auth_pattern = filterAuth;

            const data: CatalogEntryListResponse = await gatewayClient.fetchMCPCatalogEntries(params);
            setEntries(data.entries || []);
            setTotal(data.total || 0);
        } catch (err) {
            console.error('Failed to load catalog entries:', errorKind(err));
            toast.error('Load Failed', errorMessage(err, 'Could not load catalog entries.'));
            setEntries([]);
        } finally {
            setLoading(false);
            setLoadedOnce(true);
        }
    }, [gatewayClient, searchTerm, filterRuntime, filterSource, filterAuth]);

    useEffect(() => {
        runBackground(loadEntries(), 'entries');
    }, [loadEntries]);

    const handleEdit = async (entryId: string) => {
        try {
            const full: MCPCatalogEntry = await gatewayClient.fetchMCPCatalogEntry(entryId);
            setEditingEntry(full);
            setShowForm(true);
        } catch (err) {
            toast.error('Load Failed', errorMessage(err, 'Could not load entry details.'));
        }
    };

    const handleSaveEntry = async () => {
        // The form (MCPCatalogEntryForm) leaves the function that builds its payload here.
        const getPayload = (window as unknown as { __mcpCatalogFormPayload?: () => CatalogEntryCreateRequest | CatalogEntryUpdateRequest }).__mcpCatalogFormPayload;
        if (!getPayload) return;
        const payload = getPayload();

        try {
            if (editingEntry) {
                await gatewayClient.updateMCPCatalogEntry(editingEntry.id, payload);
                toast.success('Entry Updated', `${payload.name} has been updated.`);
            } else {
                await gatewayClient.createMCPCatalogEntry({ ...payload });
                toast.success('Entry Created', `${payload.name} has been created.`);
            }
            setShowForm(false);
            setEditingEntry(null);
            await loadEntries();
        } catch (err) {
            toast.error('Save Failed', errorMessage(err, 'Could not save catalog entry.'));
            throw err;
        }
    };

    const handleDelete = (entry: MCPCatalogEntryBrief) => {
        setDeleteError(null);
        setDeleteConfirmation({
            entryId: entry.id,
            entryName: entry.name,
            entryInfo: {
                id: entry.id,
                runtime: entry.runtime,
                source: entry.source,
                enabled: entry.enabled,
            },
        });
    };

    const confirmDelete = async () => {
        if (!deleteConfirmation) return;
        setIsDeleting(true);
        try {
            await gatewayClient.deleteMCPCatalogEntry(deleteConfirmation.entryId);
            toast.success('Entry Deleted', `${deleteConfirmation.entryName} has been deleted.`);
            setDeleteConfirmation(null);
            await loadEntries();
        } catch (err) {
            setDeleteError(errorMessage(err, 'Failed to delete catalog entry.'));
        } finally {
            setIsDeleting(false);
        }
    };

    const handleImportFromList = async (entryId: string) => {
        try {
            const full: MCPCatalogEntry = await gatewayClient.fetchMCPCatalogEntry(entryId);
            setImportEntry(full);
        } catch (err) {
            toast.error('Load Failed', errorMessage(err, 'Could not load entry for import.'));
        }
    };

    const handleBrowseImport = (entry: MCPCatalogEntry) => {
        setShowBrowseModal(false);
        setImportEntry(entry);
    };

    const handleImported = async () => {
        setImportEntry(null);
        await loadEntries();
    };

    const runtimeBadge = (runtime: string) => {
        switch (runtime) {
            case 'remote':
                return <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-blue-100 text-blue-700">Remote</span>;
            case 'containerized':
                return <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-purple-100 text-purple-700">Containerized</span>;
            default:
                return <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-gray-100 text-gray-700">{runtime}</span>;
        }
    };

    const authBadge = (auth: string) => {
        switch (auth) {
            case 'oauth':
                return <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-green-100 text-green-700">OAuth</span>;
            case 'api_key':
                return <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-yellow-100 text-yellow-700">API Key</span>;
            case 'header':
                return <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-orange-100 text-orange-700">Header</span>;
            case 'none':
                return <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-gray-100 text-gray-600">None</span>;
            default:
                return <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-gray-100 text-gray-600">{auth}</span>;
        }
    };

    const sourceBadge = (source: string) => {
        switch (source) {
            case 'system':
                return <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-indigo-100 text-indigo-700">System</span>;
            case 'custom':
                return <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-teal-100 text-teal-700">Custom</span>;
            default:
                return <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-gray-100 text-gray-600">{source}</span>;
        }
    };

    // Only the first load replaces the screen: a later one with no rows would
    // otherwise unmount the search box the operator is typing in.
    if (!loadedOnce) {
        return (
            <div className="p-6">
                <div className="flex justify-between items-center mb-6">
                    <div>
                        <h1 className="text-2xl font-bold text-default mb-2">MCP Server Catalog</h1>
                        <p className="text-secondary">Loading catalog entries...</p>
                    </div>
                </div>
                <div className="flex items-center justify-center h-64">
                    <div className="text-center">
                        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-primary mx-auto mb-4"></div>
                        <p className="text-secondary">Loading catalog...</p>
                    </div>
                </div>
            </div>
        );
    }

    return (
        <div className="p-6">
            {/* Header */}
            <div className="flex justify-between items-center mb-6">
                <div>
                    <h1 className="text-2xl font-bold text-default mb-2">MCP Server Catalog</h1>
                    <p className="text-secondary">
                        Manage catalog entries for quick MCP server provisioning ({entries.length} of {total})
                    </p>
                </div>
                <div className="flex items-center space-x-3">
                    <button
                        onClick={asyncHandler(loadEntries, 'click')}
                        disabled={loading}
                        className="flex items-center space-x-2 px-3 py-2 text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt disabled:opacity-50"
                    >
                        <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
                        <span>Refresh</span>
                    </button>
                    <button
                        onClick={() => setShowBrowseModal(true)}
                        className="flex items-center space-x-2 px-4 py-2 text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt transition-colors"
                    >
                        <BookOpen className="w-4 h-4" />
                        <span>Browse & Import</span>
                    </button>
                    <button
                        onClick={() => { setEditingEntry(null); setShowForm(true); }}
                        className="px-4 py-2 bg-primary-500 hover:bg-primary-600 text-white rounded-md transition-colors flex items-center space-x-2"
                        disabled={loading}
                    >
                        <Plus className="w-4 h-4" />
                        <span>Add Entry</span>
                    </button>
                </div>
            </div>

            {/* Filters and Search */}
            <div className="bg-surface rounded-lg border p-4 mb-6">
                <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4">
                    <div className="flex-1 relative min-h-0 min-w-0">
                        <Search className="w-4 h-4 absolute left-3 top-1/2 transform -translate-y-1/2 text-muted" />
                        <input
                            type="text"
                            placeholder="Search catalog entries by name or description..."
                            value={searchTerm}
                            onChange={(e) => setSearchTerm(e.target.value)}
                            className="w-full pl-9 pr-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                        />
                    </div>
                    <div className="flex items-center space-x-3">
                        <select
                            value={filterRuntime}
                            onChange={(e) => setFilterRuntime(e.target.value as any)}
                            className="px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                        >
                            <option value="all">All Runtimes</option>
                            <option value="remote">Remote</option>
                            <option value="containerized">Containerized</option>
                        </select>
                        <select
                            value={filterSource}
                            onChange={(e) => setFilterSource(e.target.value as any)}
                            className="px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                        >
                            <option value="all">All Sources</option>
                            <option value="system">System</option>
                            <option value="custom">Custom</option>
                        </select>
                        <select
                            value={filterAuth}
                            onChange={(e) => setFilterAuth(e.target.value as any)}
                            className="px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                        >
                            <option value="all">All Auth</option>
                            <option value="oauth">OAuth</option>
                            <option value="api_key">API Key</option>
                            <option value="header">Header</option>
                            <option value="none">None</option>
                        </select>
                    </div>
                </div>
            </div>

            {/* Table */}
            <div className="bg-surface rounded-lg border">
                {entries.length === 0 ? (
                    <div className="text-center py-12">
                        <Package className="w-12 h-12 text-muted mx-auto mb-4" />
                        <h3 className="text-lg font-medium text-default mb-2">
                            {total === 0 ? 'No catalog entries' : 'No entries match your filters'}
                        </h3>
                        <p className="text-secondary mb-4">
                            {total === 0
                                ? 'Get started by adding your first catalog entry or browsing available servers.'
                                : 'Try adjusting your search or filter criteria.'}
                        </p>
                        {total === 0 && (
                            <button
                                onClick={() => { setEditingEntry(null); setShowForm(true); }}
                                className="px-4 py-2 bg-primary-500 hover:bg-primary-600 text-white rounded-md transition-colors flex items-center space-x-2 mx-auto"
                            >
                                <Plus className="w-4 h-4" />
                                <span>Add First Entry</span>
                            </button>
                        )}
                    </div>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="min-w-full divide-y divide-default">
                            <thead className="bg-surface-alt">
                                <tr>
                                    <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>Name</th>
                                    <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>Runtime</th>
                                    <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>Auth</th>
                                    <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>Categories</th>
                                    <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>Source</th>
                                    <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>Enabled</th>
                                    <th className={`${TABLE_TH_TYPE} px-6 py-3 text-right`}>Actions</th>
                                </tr>
                            </thead>
                            <tbody className="bg-surface divide-y divide-default">
                                {entries.map((entry) => (
                                    <tr key={entry.id} className="hover:bg-surface-alt transition-colors">
                                        <td className="px-6 py-4">
                                            <div className="flex items-center space-x-3">
                                                <div className="w-8 h-8 rounded bg-gradient-to-br from-primary-500 to-primary-700 flex items-center justify-center flex-shrink-0">
                                                    {entry.icon_url ? (
                                                        <img
                                                            src={entry.icon_url}
                                                            alt={entry.name}
                                                            className="w-8 h-8 rounded object-contain bg-surface-alt"
                                                            onError={(e) => {
                                                                (e.target as HTMLImageElement).style.display = 'none';
                                                            }}
                                                        />
                                                    ) : (
                                                        <span className="text-white font-bold text-sm">
                                                            {entry.name.charAt(0).toUpperCase()}
                                                        </span>
                                                    )}
                                                </div>
                                                <div>
                                                    <div className="text-sm font-medium text-default">{entry.name}</div>
                                                    {entry.short_description && (
                                                        <div className="text-xs text-secondary truncate max-w-xs">
                                                            {entry.short_description}
                                                        </div>
                                                    )}
                                                </div>
                                            </div>
                                        </td>
                                        <td className="px-6 py-4">{runtimeBadge(entry.runtime)}</td>
                                        <td className="px-6 py-4">{authBadge(entry.auth_pattern)}</td>
                                        <td className="px-6 py-4">
                                            <div className="flex flex-wrap gap-1">
                                                {(entry.categories || []).slice(0, 2).map((cat) => (
                                                    <span key={cat} className="px-1.5 py-0.5 text-xs bg-surface-alt text-secondary rounded">
                                                        {cat}
                                                    </span>
                                                ))}
                                                {entry.categories && entry.categories.length > 2 && (
                                                    <span className="text-xs text-muted">+{entry.categories.length - 2}</span>
                                                )}
                                            </div>
                                        </td>
                                        <td className="px-6 py-4">{sourceBadge(entry.source)}</td>
                                        <td className="px-6 py-4">
                                            {entry.enabled ? (
                                                <CheckCircle className="w-4 h-4 text-green-600" />
                                            ) : (
                                                <XCircle className="w-4 h-4 text-red-500" />
                                            )}
                                        </td>
                                        <td className="px-6 py-4 text-right">
                                            <div className="flex items-center justify-end space-x-2">
                                                <button
                                                    onClick={asyncHandler(() => handleImportFromList(entry.id), 'click')}
                                                    className="p-1.5 text-secondary hover:text-primary-600 hover:bg-primary-50 rounded transition-colors"
                                                    title="Import as MCP Server"
                                                >
                                                    <Download className="w-4 h-4" />
                                                </button>
                                                <button
                                                    onClick={asyncHandler(() => handleEdit(entry.id), 'click')}
                                                    className="p-1.5 text-secondary hover:text-default hover:bg-surface-alt rounded transition-colors"
                                                    title="Edit entry"
                                                >
                                                    <Edit className="w-4 h-4" />
                                                </button>
                                                <button
                                                    onClick={() => handleDelete(entry)}
                                                    className="p-1.5 text-secondary hover:text-red-600 hover:bg-red-50 rounded transition-colors"
                                                    title="Delete entry"
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
                )}
            </div>

            {/* Form Modal */}
            <MCPCatalogEntryForm
                isOpen={showForm}
                onClose={() => { setShowForm(false); setEditingEntry(null); }}
                onSave={handleSaveEntry}
                entry={editingEntry || undefined}
            />

            {/* Browse Modal */}
            <MCPCatalogBrowseModal
                isOpen={showBrowseModal}
                onClose={() => setShowBrowseModal(false)}
                onImport={handleBrowseImport}
            />

            {/* Import Modal */}
            {importEntry && (
                <MCPCatalogImportModal
                    isOpen={!!importEntry}
                    onClose={() => setImportEntry(null)}
                    entry={importEntry}
                    onImported={asyncHandler(handleImported, 'imported')}
                />
            )}

            {/* Delete Confirmation Modal */}
            <GenericDeleteConfirmationModal
                isOpen={!!deleteConfirmation}
                onClose={() => {
                    if (!isDeleting) {
                        setDeleteConfirmation(null);
                        setDeleteError(null);
                    }
                }}
                onConfirm={asyncHandler(confirmDelete, 'confirm')}
                itemType="config"
                itemName={deleteConfirmation?.entryName || ''}
                itemInfo={deleteConfirmation?.entryInfo}
                isDeleting={isDeleting}
                errorMessage={deleteError}
            />
        </div>
    );
};

export default MCPCatalogTab;

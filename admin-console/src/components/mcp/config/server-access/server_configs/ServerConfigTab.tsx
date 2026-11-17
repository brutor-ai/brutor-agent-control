/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import {
    Link as LinkIcon,
    Plus,
    RefreshCw,
    AlertCircle,
    Loader,
    Search,
    Filter,
    Grid,
    List
} from 'lucide-react';
import { useGateway } from '../../../../../hooks/useGateway.ts';
import { useLocalStorageString } from '../../../../../hooks/useLocalStorage.ts';
import { useToast } from '../../../../common/Toast.tsx';
import ServerConfigRow from './ServerConfigRow.tsx';
import ServerConfigForm, { type ServerConfigPayload } from './ServerConfigForm.tsx';
import ServerConfigListView from './ServerConfigListView.tsx';
import GenericDeleteConfirmationModal from '../../../../common/GenericDeleteConfirmationModal.tsx';
import InfoBanner from '../../../../common/InfoBanner.tsx';
import { ServerConfig } from '../../../../../lib/types.ts';
import { GroupHierarchyTree } from '../../../../../lib/groupTypes.ts';
import { errorKind, errorMessage } from '../../../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../../../lib/asyncSafe';

const ServerConfigTab: React.FC = () => {
    const {
        fetchServerConfigs,
        createServerConfig,
        updateServerConfig,
        deleteServerConfig,
        fetchServers,
        // VMCP-backed configs need the VMCP list too
        // so the form can offer a VMCP picker.
        fetchVirtualServers,
        fetchOAuthAppClients,
        fetchGroupHierarchyTree
    } = useGateway();

    const toast = useToast();

    const [configs, setConfigs] = useState<ServerConfig[]>([]);
    const [servers, setServers] = useState<any[]>([]);
    // List of VMCPs the operator can bind to a server
    // config. Loaded in parallel with the regular MCP list.
    const [virtualServers, setVirtualServers] = useState<any[]>([]);
    const [oauthClients, setOauthClients] = useState<any[]>([]);
    const [groupTree, setGroupTree] = useState<GroupHierarchyTree[]>([]);  // ← STORE TREE
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [searchTerm, setSearchTerm] = useState('');
    const [filterServer, setFilterServer] = useState<string>('all');
    const [filterEnabled, setFilterEnabled] = useState<string>('all');

    // Persist view mode using custom hook
    const [viewMode, setViewMode] = useLocalStorageString('server-config-view-mode', 'grid') as ['grid' | 'list', (value: 'grid' | 'list') => void];

    const [showCreateModal, setShowCreateModal] = useState(false);
    const [editingConfig, setEditingConfig] = useState<ServerConfig | null>(null);

    // Delete confirmation modal state
    const [deleteConfirmation, setDeleteConfirmation] = useState<{
        configId: string;
        configName: string;
        configInfo: any;
    } | null>(null);
    const [isDeleting, setIsDeleting] = useState(false);
    const [deleteError, setDeleteError] = useState<string | null>(null);

    useEffect(() => {
        runBackground(loadData(), 'data');
    }, []);

    const loadData = async () => {
        setLoading(true);
        setError('');
        try {
            const [
                configsResponse,
                serversResponse,
                virtualServersResponse,
                clientsResponse,
                treeResponse,
            ] = await Promise.all([
                fetchServerConfigs(),
                fetchServers(),
                fetchVirtualServers(),
                fetchOAuthAppClients(),
                fetchGroupHierarchyTree()
            ]);

            setConfigs(configsResponse.configs || []);
            setServers(serversResponse || []);
            // Only show ENABLED VMCPs in the picker.
            // Disabled VMCPs can't be bound; operators re-enable
            // the VMCP first, then create the config.
            setVirtualServers((virtualServersResponse || []).filter((v: any) => v.enabled));
            setOauthClients(clientsResponse.clients || []);
            setGroupTree(treeResponse.tree || []);  // ← Extract tree from response
        } catch (err) {
            console.error('Failed to load data:', errorKind(err));
            setError(errorMessage(err, 'Failed to load server configurations'));
            toast.error('Failed to Load Server Configs', errorMessage(err, 'Unable to load server configurations'));
        } finally {
            setLoading(false);
        }
    };

    const handleCreate = () => {
        setEditingConfig(null);
        setShowCreateModal(true);
    };

    const handleEdit = (config: ServerConfig) => {
        setEditingConfig(config);
        setShowCreateModal(true);
    };

    const handleDelete = async (configId: string) => {
        const config = configs.find(c => c.id === configId);

        if (!config) {
            toast.error('Config Not Found', 'Unable to locate the server configuration to delete.');
            return;
        }

        // Show delete confirmation modal
        setDeleteError(null);
        setDeleteConfirmation({
            configId: config.id,
            configName: config.name,
            configInfo: {
                id: config.id,
                server_name: config.mcp_server?.name || getServerName(config.mcp_server_id),
                oauth_client_name: config.oauth_client?.name || getOAuthClientName(config.oauth_client_id),
                priority: config.priority,
                enabled: config.enabled,
                is_default: config.is_default,
                usage_count: config.usage_count
            }
        });
    };

    const confirmDeleteConfig = async () => {
        if (!deleteConfirmation) return;

        const { configId, configName } = deleteConfirmation;
        setIsDeleting(true);

        try {
            await deleteServerConfig(configId);
            await loadData();

            toast.success('Server Config Deleted', `${configName} has been deleted successfully.`);
            setDeleteConfirmation(null);
        } catch (err: any) {
            console.error('Failed to delete config:', errorKind(err));
            setDeleteError(err.detail || err.message || `Unable to delete ${configName}.`);
        } finally {
            setIsDeleting(false);
        }
    };

    // Failures propagate to the form, which is what surfaces them to the user.
    const handleSave = async (configData: ServerConfigPayload) => {
        if (editingConfig) {
            // The backend and the OAuth client are fixed at creation.
            const { mcp_server_id, virtual_server_id, oauth_client_id, ...update } = configData;
            await updateServerConfig(editingConfig.id, update);
            toast.success('Server Config Updated', `${configData.name} has been updated successfully.`);
        } else {
            await createServerConfig(configData);
            toast.success('Server Config Created', `${configData.name} has been created successfully.`);
        }
        setShowCreateModal(false);
        setEditingConfig(null);
        await loadData();
    };

    // Helpers tolerate null/undefined backend ids
    // (VMCP-backed configs leave mcp_server_id null; MCP-backed
    // configs may not have an oauth client).
    const getServerName = (serverId?: string | null): string => {
        if (!serverId) return '';
        const server = servers.find(s => s.id === serverId);
        return server ? server.name : serverId;
    };

    const getVirtualServerName = (vserverId?: string | null): string => {
        if (!vserverId) return '';
        const v = virtualServers.find((vs: any) => vs.id === vserverId);
        return v ? v.name : vserverId;
    };

    const getOAuthClientName = (clientId?: string | null): string => {
        if (!clientId) return '';
        const client = oauthClients.find(c => c.id === clientId);
        return client ? client.name : clientId;
    };

    const filteredConfigs = configs.filter(config => {
        // Search across config name + backend name (MCP or VMCP) +
        // OAuth client name.
        const backendName = config.virtual_server_id
            ? getVirtualServerName(config.virtual_server_id)
            : getServerName(config.mcp_server_id);
        const matchesSearch =
            config.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
            backendName.toLowerCase().includes(searchTerm.toLowerCase()) ||
            getOAuthClientName(config.oauth_client_id).toLowerCase().includes(searchTerm.toLowerCase());

        // Server filter matches either an MCP server id or a VMCP id.
        const matchesServer = filterServer === 'all'
            || config.mcp_server_id === filterServer
            || config.virtual_server_id === filterServer;
        const matchesEnabled =
            filterEnabled === 'all' ||
            (filterEnabled === 'enabled' && config.enabled) ||
            (filterEnabled === 'disabled' && !config.enabled);

        return matchesSearch && matchesServer && matchesEnabled;
    });

    if (loading) {
        return (
            <div className="p-6">
                <div className="text-center py-12">
                    <Loader className="w-12 h-12 animate-spin text-primary mx-auto mb-4" />
                    <p className="text-secondary">Loading Server Configurations...</p>
                </div>
            </div>
        );
    }

    return (
        <div className="p-6">
            {/* Header */}
            <div className="mb-6">
                <div className="flex justify-between items-center mb-4">
                    <div>
                        <h1 className="text-2xl font-bold text-default mb-2">MCP / VMCP Server Access Configuration</h1>
                        <p className="text-secondary">
                            Bind an MCP server <em>or</em> a Virtual MCP (VMCP) to one or more
                            resource groups. MCP configs carry an OAuth app client or run in
                            global no-auth mode; VMCP configs delegate per-call auth to the
                            underlying MCPs they compose.
                        </p>
                    </div>
                    <div className="flex items-center space-x-2">
                        <button
                            onClick={asyncHandler(loadData, 'click')}
                            className="flex items-center space-x-2 px-4 py-2 text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt transition-colors"
                        >
                            <RefreshCw className="w-4 h-4" />
                            <span>Refresh</span>
                        </button>
                        <button
                            onClick={handleCreate}
                            className="px-4 py-2 bg-primary-500 hover:bg-primary-600 text-white rounded-md transition-colors flex items-center space-x-2"
                        >
                            <Plus className="w-4 h-4" />
                            <span>Create Server Config</span>
                        </button>
                    </div>
                </div>

                {/* Search, Filters, and View Toggle */}
                <div className="flex items-center space-x-4 mb-4">
                    <div className="flex-1 relative min-h-0 min-w-0">
                        <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted" />
                        <input
                            type="text"
                            value={searchTerm}
                            onChange={(e) => setSearchTerm(e.target.value)}
                            placeholder="Search by name, server, or OAuth client..."
                            className="w-full pl-10 pr-4 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                        />
                    </div>
                    <div className="flex items-center space-x-2">
                        <Filter className="w-4 h-4 text-muted" />
                        <select
                            value={filterServer}
                            onChange={(e) => setFilterServer(e.target.value)}
                            className="px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                        >
                            <option value="all">All Servers</option>
                            {servers.map((server) => (
                                <option key={server.id} value={server.id}>
                                    {server.name}
                                </option>
                            ))}
                        </select>
                        <select
                            value={filterEnabled}
                            onChange={(e) => setFilterEnabled(e.target.value)}
                            className="px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                        >
                            <option value="all">All Status</option>
                            <option value="enabled">Enabled Only</option>
                            <option value="disabled">Disabled Only</option>
                        </select>

                        {/* View Mode Toggle */}
                        <div className="flex items-center border border-strong rounded-md overflow-hidden">
                            <button
                                onClick={() => setViewMode('grid')}
                                className={`px-3 py-2 transition-colors ${
                                    viewMode === 'grid'
                                        ? 'bg-primary-50 text-primary-700'
                                        : 'bg-surface text-secondary hover:bg-surface-alt'
                                }`}
                                title="Card View"
                            >
                                <Grid className="w-4 h-4" />
                            </button>
                            <button
                                onClick={() => setViewMode('list')}
                                className={`px-3 py-2 transition-colors border-l border-strong ${
                                    viewMode === 'list'
                                        ? 'bg-primary-50 text-primary-700'
                                        : 'bg-surface text-secondary hover:bg-surface-alt'
                                }`}
                                title="List View"
                            >
                                <List className="w-4 h-4" />
                            </button>
                        </div>
                    </div>
                </div>
            </div>

            {/* Error Display */}
            {error && (
                <div className="mb-6 p-4 bg-red-50 border border-red-200 rounded-md">
                    <div className="flex items-center space-x-2">
                        <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0" />
                        <span className="text-red-700">{error}</span>
                        <button
                            onClick={() => setError('')}
                            className="ml-auto text-red-600 hover:text-red-800"
                        >
                            ×
                        </button>
                    </div>
                </div>
            )}

            {/* Info Banner — canonical reference. All other info
                banners across admin + portal use the InfoBanner
                component so they look identical to this one. */}
            <InfoBanner title="About MCP / VMCP Server Access Configurations">
                <p>
                    Server access configs define how end-users reach MCP servers and
                    Virtual MCPs through the gateway. Each config binds a backend
                    (real MCP or VMCP) to one or more resource groups with
                    hierarchical inheritance — child groups inherit from parents
                    unless inheritance is disabled. MCP-backed configs carry an
                    OAuth app client (or run in global no-auth mode); VMCP-backed
                    configs delegate per-call auth to the underlying MCPs they
                    compose, so no separate OAuth client is needed at the VMCP
                    scope.
                </p>
            </InfoBanner>

            {/* Configurations Display - Grid or List View */}
            {filteredConfigs.length === 0 ? (
                <div className="text-center py-12 bg-surface rounded-lg border">
                    <LinkIcon className="w-12 h-12 text-muted mx-auto mb-4" />
                    <h3 className="text-lg font-medium text-default mb-2">
                        {searchTerm || filterServer !== 'all' || filterEnabled !== 'all'
                            ? 'No configurations found'
                            : 'No server configurations created'}
                    </h3>
                    <p className="text-secondary mb-4">
                        {searchTerm || filterServer !== 'all' || filterEnabled !== 'all'
                            ? 'Try adjusting your search or filters'
                            : 'Create your first server configuration to enable MCP server access'}
                    </p>
                </div>
            ) : viewMode === 'grid' ? (
                /* Grid View (Card Layout) */
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                    {filteredConfigs.map((config) => (
                        <ServerConfigRow
                            key={config.id}
                            config={config}
                            serverName={config.mcp_server?.name || (config.virtual_server_id
                                ? getVirtualServerName(config.virtual_server_id)
                                : getServerName(config.mcp_server_id))}
                            oauthClientName={config.oauth_client?.name || getOAuthClientName(config.oauth_client_id)}
                            oauthClientAppType={config.oauth_client?.app_type}
                            onEdit={handleEdit}
                            onDelete={asyncHandler(handleDelete, 'delete')}
                        />
                    ))}
                </div>
            ) : (
                /* List View (Table Layout) */
                <ServerConfigListView
                    configs={filteredConfigs}
                    servers={servers}
                    virtualServers={virtualServers}
                    oauthClients={oauthClients}
                    onEdit={handleEdit}
                    onDelete={asyncHandler(handleDelete, 'delete')}
                />
            )}

            {/* Create/Edit Modal */}
            {showCreateModal && (
                <ServerConfigForm
                    config={editingConfig}
                    servers={servers}
                    virtualServers={virtualServers}
                    oauthClients={oauthClients.filter(c => c.is_active)}
                    groupTree={groupTree}
                    onSave={handleSave}
                    onCancel={() => {
                        setShowCreateModal(false);
                        setEditingConfig(null);
                    }}
                />
            )}

            {/* Delete Confirmation Modal */}
            {deleteConfirmation && (
                <GenericDeleteConfirmationModal
                    isOpen={true}
                    onClose={() => {
                        if (!isDeleting) {
                            setDeleteConfirmation(null);
                            setDeleteError(null);
                        }
                    }}
                    onConfirm={asyncHandler(confirmDeleteConfig, 'confirm')}
                    itemType="config"
                    itemName={deleteConfirmation.configName}
                    itemInfo={{
                        id: deleteConfirmation.configInfo.id,
                        description: `${deleteConfirmation.configInfo.server_name} → ${deleteConfirmation.configInfo.oauth_client_name}`,
                        enabled: deleteConfirmation.configInfo.enabled,
                    }}
                    customWarning="Users will lose access to this server through the configured OAuth client."
                    customNote={`This configuration has been used ${deleteConfirmation.configInfo.usage_count} time${deleteConfirmation.configInfo.usage_count !== 1 ? 's' : ''}. Priority: ${deleteConfirmation.configInfo.priority}${deleteConfirmation.configInfo.is_default ? ' (Default)' : ''}`}
                    isDeleting={isDeleting}
                    errorMessage={deleteError}
                />
            )}
        </div>
    );
};

export default ServerConfigTab;
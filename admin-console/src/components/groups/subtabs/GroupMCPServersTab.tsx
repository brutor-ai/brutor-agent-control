/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useCallback } from 'react';
import {
    Loader,
    AlertCircle,
    Search,
    X,
    ArrowUpRight,
    CheckCircle,
    Info,
    HelpCircle
} from 'lucide-react';
import MCPIcon from '../../common/MCPIcon';
import { ResourceGroup, ServerConfigInfo, EffectiveServerConfigsResponse, formatGroupLabel } from '../../../lib/groupTypes';
import { useGateway } from '../../../hooks/useGateway';
import { useToast } from '../../common/Toast';
import GenericDeleteConfirmationModal from '../../common/GenericDeleteConfirmationModal';
import GroupMCPServersTableView from './GroupMCPServersTableView';
import MCPCapabilityFilterDialog from './MCPCapabilityFilterDialog';
import MCPServerLimitsDialog from './MCPServerLimitsDialog';
import MCPToolLimitsDialog from './MCPToolLimitsDialog';
import { errorKind, errorMessage } from '../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';

interface Props {
    group: ResourceGroup;
    onUpdate: () => void;
}

type ViewMode = 'all' | 'direct' | 'inherited';

const GroupMCPServersTab: React.FC<Props> = ({ group, onUpdate }) => {
    const { fetchEffectiveServerConfigs, removeServerFromGroup } = useGateway();
    const toast = useToast();

    const [effectiveServers, setEffectiveServers] = useState<EffectiveServerConfigsResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [searchTerm, setSearchTerm] = useState('');
    const [viewMode, setViewMode] = useState<ViewMode>('all');
    const [removingId, setRemovingId] = useState<string | null>(null);
    const [deleteError, setDeleteError] = useState<string | null>(null);

    // Info modal state
    const [showInheritanceInfo, setShowInheritanceInfo] = useState(false);
    const [showAddServersInfo, setShowAddServersInfo] = useState(false);

    // Delete confirmation state
    const [deleteConfirmation, setDeleteConfirmation] = useState<{
        serverId: string;
        serverName: string;
    } | null>(null);

    // Capability filter dialog state
    const [capabilityServer, setCapabilityServer] = useState<ServerConfigInfo | null>(null);

    // Limits dialog state
    const [limitsServer, setLimitsServer] = useState<ServerConfigInfo | null>(null);
    const [toolLimitsServer, setToolLimitsServer] = useState<ServerConfigInfo | null>(null);

    const loadServers = useCallback(async () => {
        try {
            setLoading(true);
            setError('');
            const response = await fetchEffectiveServerConfigs(group.id);
            setEffectiveServers(response);
        } catch (err) {
            console.error('Failed to load servers:', errorKind(err));
            setError(errorMessage(err, 'Failed to load MCP servers'));
        } finally {
            setLoading(false);
        }
    }, [group.id, fetchEffectiveServerConfigs]);

    useEffect(() => {
        runBackground(loadServers(), 'servers');
    }, [loadServers]);

    const handleRemoveServer = (serverId: string, serverName: string) => {
        setDeleteError(null);
        setDeleteConfirmation({
            serverId,
            serverName
        });
    };

    const confirmRemoveServer = async () => {
        if (!deleteConfirmation) return;

        const { serverId, serverName } = deleteConfirmation;

        try {
            setRemovingId(serverId);
            setError('');
            await removeServerFromGroup(group.id, serverId);
            await loadServers();
            onUpdate();
            toast.success(`Removed ${serverName} from ${formatGroupLabel(group)}`);
            setDeleteConfirmation(null);
        } catch (err: any) {
            console.error('Failed to remove server:', errorKind(err));
            setDeleteError(err.detail || err.message || 'Failed to remove server');
        } finally {
            setRemovingId(null);
        }
    };

    // Get servers based on view mode
    const getDisplayServers = (): ServerConfigInfo[] => {
        if (!effectiveServers) return [];

        switch (viewMode) {
            case 'direct':
                return effectiveServers.direct_configs || [];
            case 'inherited':
                return effectiveServers.inherited_configs || [];
            case 'all':
            default:
                return effectiveServers.effective_configs || [];
        }
    };

    // Filter servers by search term
    const displayServers = getDisplayServers();

    const filteredServers = displayServers.filter(server => {
        const matches = server.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
            server.id.toLowerCase().includes(searchTerm.toLowerCase());
        return matches;
    });

    return (
        <div className="p-6 space-y-6">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div>
                    <h2 className="text-lg font-semibold text-default">MCP Servers</h2>
                    <p className="text-sm text-secondary mt-1">
                        Model Context Protocol servers available to this resource group.
                    </p>
                </div>
                <div className="flex items-center space-x-2">
                    <button
                        onClick={() => setShowInheritanceInfo(true)}
                        className="flex items-center space-x-2 px-3 py-2 text-sm text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt transition-colors"
                        title="Learn about server inheritance"
                    >
                        <HelpCircle className="w-4 h-4" />
                        <span>Inheritance</span>
                    </button>
                    <button
                        onClick={() => setShowAddServersInfo(true)}
                        className="flex items-center space-x-2 px-3 py-2 text-sm text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt transition-colors"
                        title="Learn how to add servers"
                    >
                        <HelpCircle className="w-4 h-4" />
                        <span>Add Servers</span>
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
                        placeholder="Search servers..."
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
                <div className="flex items-center space-x-1 bg-surface-raised rounded-lg p-1 overflow-x-auto">
                    <button
                        onClick={() => setViewMode('all')}
                        className={`px-3 py-2 rounded-lg text-sm font-medium transition-colors whitespace-nowrap ${
                            viewMode === 'all'
                                ? 'bg-surface text-default shadow-sm'
                                : 'text-secondary hover:text-default'
                        }`}
                    >
                        All ({effectiveServers?.effective_count || 0})
                    </button>
                    <button
                        onClick={() => setViewMode('direct')}
                        className={`px-3 py-2 rounded-lg text-sm font-medium transition-colors whitespace-nowrap ${
                            viewMode === 'direct'
                                ? 'bg-surface text-default shadow-sm'
                                : 'text-secondary hover:text-default'
                        }`}
                    >
                        Direct ({effectiveServers?.direct_count || 0})
                    </button>
                    <button
                        onClick={() => setViewMode('inherited')}
                        className={`px-3 py-2 rounded-lg text-sm font-medium transition-colors whitespace-nowrap ${
                            viewMode === 'inherited'
                                ? 'bg-surface text-default shadow-sm'
                                : 'text-secondary hover:text-default'
                        }`}
                    >
                        Inherited ({effectiveServers?.inherited_count || 0})
                    </button>
                </div>
            </div>

            {/* Info Banner for Inherited View */}
            {viewMode === 'inherited' && effectiveServers && effectiveServers.inherited_count > 0 && (
                <div className="flex items-start space-x-3 p-4 bg-surface-alt border border-default rounded-lg">
                    <Info className="w-5 h-5 text-muted flex-shrink-0 mt-0.5" />
                    <div className="flex-1 min-h-0 min-w-0">
                        <p className="text-sm font-medium text-secondary">Inherited Servers</p>
                        <p className="text-sm text-secondary mt-1">
                            These servers are inherited from parent groups in the hierarchy. To modify them, edit the parent group.
                        </p>
                    </div>
                </div>
            )}

            {/* Servers Table */}
            {loading ? (
                <div className="flex items-center justify-center h-48">
                    <div className="text-center">
                        <Loader className="w-8 h-8 text-primary-600 animate-spin mx-auto mb-2" />
                        <p className="text-sm text-secondary">Loading servers...</p>
                    </div>
                </div>
            ) : filteredServers.length === 0 ? (
                <div className="text-center py-12 bg-surface-alt rounded-lg border-2 border-dashed border-strong">
                    <MCPIcon className="w-12 h-12 text-muted mx-auto mb-3" />
                    <p className="text-default font-medium mb-1">
                        {searchTerm
                            ? 'No servers found matching your search'
                            : viewMode === 'direct'
                                ? 'No direct servers assigned yet'
                                : viewMode === 'inherited'
                                    ? 'No inherited servers from parent groups'
                                    : 'No MCP servers available'}
                    </p>
                    {!searchTerm && (
                        <p className="text-sm text-muted mt-2">
                            {viewMode === 'direct'
                                ? 'Click "Add Servers" to assign MCP servers directly to this group'
                                : viewMode === 'inherited'
                                    ? 'Servers will appear here when assigned to parent groups'
                                    : 'Add servers from the MCP Servers section or assign them to parent groups'}
                        </p>
                    )}
                </div>
            ) : (
                <GroupMCPServersTableView
                    servers={filteredServers}
                    onRemove={handleRemoveServer}
                    onConfigureCapabilities={(server) => setCapabilityServer(server)}
                    onConfigureLimits={setLimitsServer}
                    onConfigureToolLimits={setToolLimitsServer}
                    removingId={removingId}
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
                    onConfirm={asyncHandler(confirmRemoveServer, 'confirm')}
                    action="remove"
                    itemType="server"
                    itemName={deleteConfirmation.serverName}
                    itemInfo={{
                        id: deleteConfirmation.serverId,
                        description: 'MCP Server',
                        enabled: true,
                    }}
                    customWarning={`This will remove ${deleteConfirmation.serverName} from ${formatGroupLabel(group)}. Members will no longer have access to this server.`}
                    customNote="The server will still be available in the system and can be re-added later."
                    isDeleting={removingId === deleteConfirmation.serverId}
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
                                    Server Inheritance
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
                                    What is Server Inheritance?
                                </h3>
                                <p className="text-sm text-secondary">
                                    Resource groups can inherit MCP servers from their parent groups in the hierarchy.
                                    This means servers assigned to parent groups automatically become available to all descendant groups.
                                </p>
                            </div>

                            <div className="bg-surface-alt border border-default rounded-lg p-4">
                                <h4 className="text-sm font-semibold text-secondary mb-2">
                                    Server Types
                                </h4>
                                <ul className="space-y-2 text-sm text-secondary">
                                    <li className="flex items-start space-x-2">
                                        <CheckCircle className="w-4 h-4 text-primary flex-shrink-0 mt-0.5" />
                                        <span><strong>Direct Servers:</strong> Servers explicitly assigned to this group</span>
                                    </li>
                                    <li className="flex items-start space-x-2">
                                        <ArrowUpRight className="w-4 h-4 text-muted flex-shrink-0 mt-0.5" />
                                        <span><strong>Inherited Servers:</strong> Servers from parent groups (shown with light blue background)</span>
                                    </li>
                                </ul>
                            </div>

                            <div>
                                <h4 className="text-sm font-semibold text-default mb-2">
                                    Managing Inherited Servers
                                </h4>
                                <p className="text-sm text-secondary mb-2">
                                    Inherited servers cannot be removed from this group directly. To modify them, edit the parent group that owns the server.
                                </p>
                            </div>

                            <div className="bg-surface-alt border border-default rounded-lg p-4">
                                <p className="text-xs text-secondary">
                                    <strong>Example:</strong> If "Organization" group has a file system server, all teams under that organization
                                    (Engineering, Marketing, etc.) will automatically inherit access to that server.
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

            {/* Capability Filter Dialog */}
            {capabilityServer && (
                <MCPCapabilityFilterDialog
                    isOpen={true}
                    onClose={() => {
                        setCapabilityServer(null);
                        runBackground(loadServers(), 'servers');
                    }}
                    groupId={group.id}
                    groupName={formatGroupLabel(group)}
                    server={capabilityServer}
                />
            )}

            {/* Per-Server Limits Dialog */}
            {limitsServer && (
                <MCPServerLimitsDialog
                    isOpen={true}
                    onClose={() => setLimitsServer(null)}
                    onSave={asyncHandler(loadServers, 'save')}
                    groupId={group.id}
                    groupName={formatGroupLabel(group)}
                    server={limitsServer}
                />
            )}

            {/* Per-Tool Rate Limits Dialog */}
            {toolLimitsServer && (
                <MCPToolLimitsDialog
                    isOpen={true}
                    onClose={() => setToolLimitsServer(null)}
                    onSave={asyncHandler(loadServers, 'save')}
                    groupId={group.id}
                    groupName={formatGroupLabel(group)}
                    server={toolLimitsServer}
                />
            )}

            {/* Add Servers Info Modal */}
            {showAddServersInfo && (
                <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
                    <div className="bg-surface rounded-lg shadow-xl max-w-2xl w-full">
                        <div className="flex items-center justify-between p-6 border-b border-default">
                            <div className="flex items-center space-x-3">
                                <div className="p-2 bg-primary-pale rounded-lg">
                                    <HelpCircle className="w-5 h-5 text-primary" />
                                </div>
                                <h2 className="text-xl font-semibold text-default">
                                    How to Add Servers to a Resource Group
                                </h2>
                            </div>
                            <button
                                onClick={() => setShowAddServersInfo(false)}
                                className="text-muted hover:text-secondary transition-colors"
                            >
                                <X className="w-5 h-5" />
                            </button>
                        </div>
                        <div className="p-6 space-y-4">
                            <p className="text-sm text-secondary">
                                MCP servers are not added directly in the Groups interface. Instead, servers are managed
                                centrally and then linked to groups through the MCP Servers configuration page.
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
                                            <p className="font-medium">Navigate to MCP Server configuration</p>
                                            <p className="text-primary mt-1">
                                                Go to <strong>Resources | MCP Servers | Configuration</strong> in the left side navigation
                                            </p>
                                        </div>
                                    </li>
                                    <li className="flex items-start space-x-3">
                                        <span className="flex-shrink-0 w-6 h-6 bg-primary text-white rounded-full flex items-center justify-center text-xs font-bold">
                                            2
                                        </span>
                                        <div>
                                            <p className="font-medium">In the MCP Servers page add the server</p>
                                            <p className="text-primary mt-1">
                                                To add an MCP server click the <strong>+ Add Server</strong> button in the upper right corner.

                                                <p/><strong>Note.</strong> if you have already configured the MCP server, Navigate to the <strong>MCP Server Access</strong> tab.
                                            </p>
                                        </div>
                                    </li>
                                    <li className="flex items-start space-x-3">
                                        <span className="flex-shrink-0 w-6 h-6 bg-primary text-white rounded-full flex items-center justify-center text-xs font-bold">
                                            3
                                        </span>
                                        <div>
                                            <p className="font-medium">Add OAuth App Client for MCP Server (OPTIONAL)</p>
                                            <p className="text-primary mt-1">
                                                If the server requires OAuth then add the OAuth application client by clicking the <strong>+ Create OAuth App Client</strong> button.
                                            </p>
                                        </div>
                                    </li>
                                    <li className="flex items-start space-x-3">
                                        <span className="flex-shrink-0 w-6 h-6 bg-primary text-white rounded-full flex items-center justify-center text-xs font-bold">
                                            4
                                        </span>
                                        <div>
                                            <p className="font-medium">Navigate to MCP Server Access Configurations</p>
                                            <p className="text-primary mt-1">
                                                Create a server configuration by clicking the <strong>+ Create Server Config</strong> button.
                                                A server configuration defines the MCP server type, OAuth App client (if any), and the resource groups that should have access to it.
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
                                                In the server configuration edit form, select the resource groups that should have access to this server
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
                                                Click <strong>Save</strong> to apply the group assignments
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
                                            The server will appear in this group's list after you save the changes.
                                            If the group inherits from parents, the server will also automatically appear in all descendant groups.
                                        </p>
                                    </div>
                                </div>
                            </div>
                        </div>
                        <div className="flex items-center justify-end p-6 border-t border-default bg-surface-alt">
                            <button
                                onClick={() => setShowAddServersInfo(false)}
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

export default GroupMCPServersTab;
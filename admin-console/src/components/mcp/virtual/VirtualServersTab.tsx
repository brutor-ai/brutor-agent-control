/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import {
    Network,
    Plus,
    Edit,
    Trash2,
    Power,
    PowerOff,
    Search,
    RefreshCw,
    Settings,
    Server,
    Info,
    User,
    Calendar,
    Crown,
    Shield,
    ShieldAlert,
    X,
} from 'lucide-react';
import { VirtualServerConfig, MCPServerConfig } from '../../../lib/types.ts';
import VirtualServerForm from './VirtualServerForm.tsx';
import GenericDeleteConfirmationModal from '../../common/GenericDeleteConfirmationModal.tsx';
import InfoBanner from '../../common/InfoBanner.tsx';
import { useErrorHandler } from '../../../hooks/useErrorHandler.ts';
import { ErrorDisplay } from '../../common/ErrorDisplay.tsx';
import { useAuth } from '../../../hooks/useAuth.tsx';
import { useGateway } from '../../../hooks/useGateway.ts';
import { useToast } from '../../common/Toast.tsx';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import { errorKind } from '../../../lib/narrow';

interface VirtualServersTabProps {
    virtualServers?: VirtualServerConfig[];
    availableServers?: MCPServerConfig[];
    onCreateVirtualServer?: (virtualServer: Omit<VirtualServerConfig, 'created_at'>) => Promise<void>;
    onUpdateVirtualServer?: (id: string, virtualServer: Partial<VirtualServerConfig>) => Promise<void>;
    onDeleteVirtualServer?: (id: string) => Promise<void>;
    onRefresh?: () => Promise<void>;
}

interface VirtualServerWithOwnerInfo extends VirtualServerConfig {
    owner_username?: string;
    owner_display_name?: string;
    owner_roles?: string[];
}

const VirtualServersTab: React.FC<VirtualServersTabProps> = ({
                                                                 virtualServers: virtualServersProp,
                                                                 availableServers: availableServersProp,
                                                                 onCreateVirtualServer: onCreateVirtualServerProp,
                                                                 onUpdateVirtualServer: onUpdateVirtualServerProp,
                                                                 onDeleteVirtualServer: onDeleteVirtualServerProp,
                                                                 onRefresh: onRefreshProp
                                                             }) => {
    const toast = useToast();

    const { user: currentUser, hasPermission } = useAuth();
    const {
        users,
        usersLoaded,
        fetchUsers,
        getUsernameById,
        getUserDisplayName,
        getUserRoles,
        fetchServers,
        fetchVirtualServers,
        createVirtualServer: createVirtualServerGateway,
        updateVirtualServer: updateVirtualServerGateway,
        deleteVirtualServer: deleteVirtualServerGateway
    } = useGateway();

    const [localVirtualServers, setLocalVirtualServers] = useState<VirtualServerConfig[]>([]);
    const [localAvailableServers, setLocalAvailableServers] = useState<MCPServerConfig[]>([]);
    const [loading, setLoading] = useState(false);

    const virtualServers = virtualServersProp !== undefined ? virtualServersProp : localVirtualServers;
    const availableServers = availableServersProp !== undefined ? availableServersProp : localAvailableServers;
    const onCreateVirtualServer = onCreateVirtualServerProp || createVirtualServerGateway;
    const onUpdateVirtualServer = onUpdateVirtualServerProp || updateVirtualServerGateway;
    const onDeleteVirtualServer = onDeleteVirtualServerProp || deleteVirtualServerGateway;

    const [editingServer, setEditingServer] = useState<VirtualServerConfig | null>(null);
    const [showAddServer, setShowAddServer] = useState(false);
    const [searchTerm, setSearchTerm] = useState('');
    const [filterStatus, setFilterStatus] = useState<'all' | 'enabled' | 'disabled'>('all');
    const [filterOwner, setFilterOwner] = useState<'all' | 'mine' | 'others'>('all');
    const [virtualServersWithOwnerInfo, setVirtualServersWithOwnerInfo] = useState<VirtualServerWithOwnerInfo[]>([]);

    // Delete confirmation modal state
    // Populated when the VMCP create/update endpoint
    // returns `poisoned_tools` (registration-time prompt-injection
    // scan flagged one or more operator-supplied
    // description_override / exposed_name fields). Same modal
    // shape as the upstream MCP server form — see
    // ServersTab.tsx's poisonedToolsAlert for the same wiring.
    const [poisonedToolsAlert, setPoisonedToolsAlert] = useState<{
        serverName: string;
        findings: Array<{
            tool_name: string;
            field_kind?: string;
            description_excerpt: string;
            description_truncated: boolean;
            confidence: number;
            patterns_matched: number;
            suspicious_indicator_count: number;
        }>;
    } | null>(null);

    const [deleteConfirmation, setDeleteConfirmation] = useState<{
        serverId: string;
        serverName: string;
        serverInfo: any;
    } | null>(null);
    const [isDeleting, setIsDeleting] = useState(false);
    const [deleteError, setDeleteError] = useState<string | null>(null);

    const {
        errors: componentErrors = [],
        hasErrors,
        dismissError,
        clearErrors,
        handleError
    } = useErrorHandler({
        reportError: (error) => {
            console.error(`Virtual server error (${error.severity}) in ${error.context?.operation ?? 'an operation'}`);
        },
        autoDismissMs: 6000,
        maxErrors: 10
    });

    useEffect(() => {
        const loadData = async () => {
            if (virtualServersProp === undefined || availableServersProp === undefined) {
                setLoading(true);
                try {
                    if (virtualServersProp === undefined) {
                        const vs = await fetchVirtualServers();
                        setLocalVirtualServers(vs);
                    }
                    if (availableServersProp === undefined) {
                        const servers = await fetchServers();
                        setLocalAvailableServers(servers);
                    }
                } catch (error) {
                    console.error('Failed to load data:', errorKind(error));
                } finally {
                    setLoading(false);
                }
            }
        };

        runBackground(loadData(), 'data');
    }, [virtualServersProp, availableServersProp, fetchServers, fetchVirtualServers]);

    useEffect(() => {
        if (!usersLoaded && users.length === 0) {
            runBackground(fetchUsers(), 'users');
        }
    }, [usersLoaded, users.length, fetchUsers]);

    useEffect(() => {
        if (usersLoaded) {
            const serversWithOwnerInfo = virtualServers.map(vs => ({
                ...vs,
                owner_username: getUsernameById(vs.owner),
                owner_display_name: getUserDisplayName(vs.owner),
                owner_roles: getUserRoles(vs.owner)
            }));
            setVirtualServersWithOwnerInfo(serversWithOwnerInfo);
        } else {
            const serversWithBasicInfo = virtualServers.map(vs => ({
                ...vs,
                owner_username: vs.owner === currentUser?.id ? currentUser.username : 'Loading...',
                owner_display_name: vs.owner === currentUser?.id ? (currentUser.full_name || currentUser.username) : 'Loading...',
            }));
            setVirtualServersWithOwnerInfo(serversWithBasicInfo);
        }
    }, [virtualServers, usersLoaded, currentUser, getUsernameById, getUserDisplayName, getUserRoles]);

    // The permissions the Control Plane asks for: virtual:server:update to
    // enable, disable and edit a virtual server, virtual:server:delete to
    // delete one.
    const canUpdate = !!currentUser && hasPermission('virtual:server:update');
    const canDelete = !!currentUser && hasPermission('virtual:server:delete');

    const filteredServers = virtualServersWithOwnerInfo.filter(server => {
        const matchesSearch = !searchTerm ||
            server.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
            server.id?.toLowerCase().includes(searchTerm.toLowerCase()) ||
            server.description.toLowerCase().includes(searchTerm.toLowerCase()) ||
            (server.owner_username && server.owner_username.toLowerCase().includes(searchTerm.toLowerCase())) ||
            (server.owner_display_name && server.owner_display_name.toLowerCase().includes(searchTerm.toLowerCase()));

        const matchesStatus = filterStatus === 'all' ||
            (filterStatus === 'enabled' && server.enabled) ||
            (filterStatus === 'disabled' && !server.enabled);

        const matchesOwner = filterOwner === 'all' ||
            (filterOwner === 'mine' && server.owner === currentUser?.id) ||
            (filterOwner === 'others' && server.owner !== currentUser?.id);

        return matchesSearch && matchesStatus && matchesOwner;
    });

    const handleToggleServer = async (serverId: string) => {
        const server = virtualServers.find(s => s.id === serverId);
        if (!server || !onUpdateVirtualServer) {
            toast.error('Server Not Found', 'Unable to locate the virtual server.');
            return;
        }

        const action = server.enabled ? 'disabled' : 'enabled';

        await handleError(
            async () => {
                await onUpdateVirtualServer(serverId, { enabled: !server.enabled });
                if (virtualServersProp === undefined) {
                    const vs = await fetchVirtualServers();
                    setLocalVirtualServers(vs);
                }
                toast.success(
                    `Virtual Server ${action.charAt(0).toUpperCase() + action.slice(1)}`,
                    `${server.name} has been ${action}.`
                );
            },
            {
                operation: 'toggle_virtual_server',
                resource: 'virtual_server',
                serverId: serverId
            },
            {
                severity: 'medium'
            }
        );
    };

    const handleSaveServer = async (formData: Omit<VirtualServerConfig, 'created_at'>): Promise<void> => {
        await handleError(
            async () => {
                let result: any = null;
                if (editingServer && onUpdateVirtualServer) {
                    const updateData: Partial<VirtualServerConfig> = {
                        name: formData.name,
                        description: formData.description,
                        capabilities: formData.capabilities,
                        enabled: formData.enabled,
                        owner: formData.owner
                    };
                    if (!formData.id) {
                        throw new Error('Virtual server ID is required for update');
                    }
                    result = await onUpdateVirtualServer(formData.id, updateData);
                    toast.success(
                        'Virtual Server Updated',
                        `${formData.name} has been updated successfully.`
                    );
                } else if (onCreateVirtualServer) {
                    result = await onCreateVirtualServer(formData);
                    toast.success(
                        'Virtual Server Created',
                        `${formData.name} has been created successfully.`
                    );
                }

                // Surface any poison findings the
                // VMCP backend returned. Catches operator-supplied
                // exposed_name / description_override fields that
                // tripped the prompt-injection detector. Same
                // modal as the upstream MCP path.
                const findings = (result)?.poisoned_tools;
                if (Array.isArray(findings) && findings.length > 0) {
                    setPoisonedToolsAlert({
                        serverName: formData.name || 'Virtual MCP server',
                        findings,
                    });
                }

                // Reload data before closing form
                if (virtualServersProp === undefined) {
                    const vs = await fetchVirtualServers();
                    setLocalVirtualServers(vs);
                }

                // Close form only after successful save and reload
                if (editingServer) {
                    setEditingServer(null);
                } else {
                    setShowAddServer(false);
                }
            },
            {
                operation: editingServer ? 'update_virtual_server' : 'create_virtual_server',
                resource: 'virtual_server',
                serverName: formData.name
            },
            {
                severity: 'high'
            }
        );
    };

    const handleDeleteServer = async (serverId: string) => {
        const server = virtualServers.find(s => s.id === serverId);
        if (!server || !onDeleteVirtualServer) {
            toast.error('Server Not Found', 'Unable to locate the virtual server to delete.');
            return;
        }

        // Get owner info for display
        const ownerInfo = virtualServersWithOwnerInfo.find(vs => vs.id === serverId);

        // Show delete confirmation modal
        if (!server.id) {
            toast.error('Error', 'Server ID is missing');
            return;
        }
        setDeleteError(null);
        setDeleteConfirmation({
            serverId: server.id,
            serverName: server.name,
            serverInfo: {
                id: server.id,
                description: server.description,
                enabled: server.enabled,
                owner: ownerInfo?.owner_display_name || ownerInfo?.owner_username || 'Unknown',
                capabilityCount: (server.capabilities?.tools?.length || 0) +
                    (server.capabilities?.resources?.length || 0) +
                    (server.capabilities?.prompts?.length || 0)
            }
        });
    };

    const confirmDeleteServer = async () => {
        if (!deleteConfirmation || !onDeleteVirtualServer) return;

        const { serverId, serverName } = deleteConfirmation;

        setIsDeleting(true);
        setDeleteError(null);

        try {
            await onDeleteVirtualServer(serverId);
            if (virtualServersProp === undefined) {
                const vs = await fetchVirtualServers();
                setLocalVirtualServers(vs);
            }
            toast.success('Virtual Server Deleted', `${serverName} has been deleted successfully.`);
            setDeleteConfirmation(null);
        } catch (err: any) {
            console.error('Failed to delete virtual server:', errorKind(err));
            setDeleteError(err.detail || err.message || `Unable to delete ${serverName}.`);
        } finally {
            setIsDeleting(false);
        }
    };

    const handleRefresh = async () => {
        if (onRefreshProp) {
            await handleError(
                async () => {
                    await onRefreshProp();
                    await fetchUsers();
                },
                {
                    operation: 'refresh_virtual_servers',
                    resource: 'virtual_servers'
                },
                {
                    severity: 'low'
                }
            );
        } else {
            await handleError(
                async () => {
                    const [vs, servers] = await Promise.all([
                        fetchVirtualServers(),
                        fetchServers()
                    ]);
                    setLocalVirtualServers(vs);
                    setLocalAvailableServers(servers);
                    await fetchUsers();
                },
                {
                    operation: 'refresh_virtual_servers',
                    resource: 'virtual_servers'
                },
                {
                    severity: 'low'
                }
            );
        }
    };

    const handleRetryError = async (errorId: string) => {
        if (!componentErrors || componentErrors.length === 0) return;

        const error = componentErrors.find(e => e.id === errorId);
        if (!error) return;

        try {
            switch (error.context?.operation) {
                case 'refresh_virtual_servers':
                    await handleRefresh();
                    break;
                default:
                    break;
            }
        } catch (retryError) {
            console.error('Retry failed:', errorKind(retryError));
        }
    };

    const getCapabilityIcon = (type: string) => {
        switch (type) {
            case 'tools':
                return <Settings className="w-4 h-4" />;
            case 'resources':
                return <Server className="w-4 h-4" />;
            case 'prompts':
                return <Info className="w-4 h-4" />;
            default:
                return <Settings className="w-4 h-4" />;
        }
    };

    const getCapabilityColor = (type: string) => {
        switch (type) {
            case 'tools':
                return 'bg-blue-100 text-blue-800 border-blue-200';
            case 'resources':
                return 'bg-green-100 text-green-800 border-green-200';
            case 'prompts':
                return 'bg-purple-100 text-purple-800 border-purple-200';
            default:
                return 'bg-surface-raised text-default border-default';
        }
    };

    const getTotalCapabilities = (capabilities: VirtualServerConfig['capabilities']) => {
        if (!capabilities || typeof capabilities !== 'object') {
            return 0;
        }
        return Object.values(capabilities).reduce((total, caps) => total + (caps?.length || 0), 0);
    };

    const getServerName = (serverId: string) => {
        const server = availableServers.find(s => s.id === serverId);
        return server ? server.name : serverId;
    };

    const formatDate = (dateString: string) => {
        return new Date(dateString).toLocaleDateString('en-US', {
            year: 'numeric',
            month: 'short',
            day: 'numeric'
        });
    };

    const getSourceServerIds = (capabilities: VirtualServerConfig['capabilities']): string[] => {
        if (!capabilities || typeof capabilities !== 'object') {
            return [];
        }

        const serverIds = new Set<string>();
        Object.values(capabilities).forEach(capList => {
            if (Array.isArray(capList)) {
                capList.forEach((cap: any) => {
                    if (cap && typeof cap === 'object' && cap.server_id) {
                        serverIds.add(cap.server_id);
                    }
                });
            }
        });
        return Array.from(serverIds);
    };

    const getRoleBadge = (roles: string[]) => {
        if (roles.includes('super_admin')) {
            return (
                <div className="inline-flex items-center space-x-1 px-2 py-1 bg-red-100 text-red-700 rounded text-xs font-medium">
                    <Crown className="w-3 h-3" />
                    <span>Admin</span>
                </div>
            );
        } else if (roles.includes('virtual_server_admin')) {
            return (
                <div className="inline-flex items-center space-x-1 px-2 py-1 bg-blue-100 text-blue-700 rounded text-xs font-medium">
                    <Shield className="w-3 h-3" />
                    <span>VS Admin</span>
                </div>
            );
        } else if (roles.includes('virtual_server_manager')) {
            return (
                <div className="inline-flex items-center space-x-1 px-2 py-1 bg-green-100 text-green-700 rounded text-xs font-medium">
                    <User className="w-3 h-3" />
                    <span>Manager</span>
                </div>
            );
        }
        return null;
    };

    if (loading && virtualServers.length === 0) {
        return (
            <div className="p-6">
                <div className="flex justify-between items-center mb-6">
                    <div>
                        <h1 className="text-2xl font-bold text-default mb-2">Virtual Servers</h1>
                        <p className="text-secondary">Loading virtual server configurations...</p>
                    </div>
                </div>
                <div className="flex items-center justify-center h-64">
                    <div className="text-center">
                        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600 mx-auto mb-4"></div>
                        <p className="text-secondary">Loading virtual servers...</p>
                    </div>
                </div>
            </div>
        );
    }

    return (
        <div className="p-6">
            <div className="flex justify-between items-center mb-6">
                <div>
                    <h1 className="text-2xl font-bold text-default mb-2">Virtual Servers</h1>
                    <p className="text-secondary">
                        {filteredServers.length} of {virtualServers.length} configurations
                    </p>
                </div>
                <div className="flex items-center space-x-3">
                    <button
                        onClick={asyncHandler(handleRefresh, 'click')}
                        disabled={loading || (hasErrors && componentErrors?.some(e => e.context?.operation === 'refresh_virtual_servers'))}
                        className="flex items-center space-x-2 px-3 py-2 text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt disabled:opacity-50 transition-colors"
                    >
                        <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
                        <span>Refresh</span>
                    </button>
                    <button
                        onClick={() => setShowAddServer(true)}
                        className="px-4 py-2 bg-primary-500 hover:bg-primary-600 text-white rounded-md transition-colors flex items-center space-x-2 disabled:opacity-50 disabled:cursor-not-allowed"
                        disabled={availableServers.length === 0 || loading}
                        title={availableServers.length === 0 ? "No MCP servers available to create virtual servers" : "Create Virtual Server"}
                    >
                        <Plus className="w-4 h-4" />
                        <span>Add Virtual Server</span>
                    </button>
                </div>
            </div>

            <InfoBanner title="About Virtual MCP Servers">
                <p>
                    A Virtual MCP (VMCP) composes a curated tool surface from one or
                    more upstream MCP servers — aggregating, renaming, and trimming
                    tools, then re-exposing them as a single server endpoint at{' '}
                    <code className="px-1 py-0.5 rounded bg-blue-100 font-mono text-xs">/v1/proxy/vmcp/&#123;id&#125;</code>.
                    Each <code className="font-mono text-xs">tools/call</code> is delegated back to the originating
                    upstream — including its OAuth credentials, rate limits, and per-tool
                    guardrails — so the runtime stays consistent with calling the real
                    server directly. To expose a VMCP to end users, create an MCP Server
                    Access configuration with backend <span className="font-medium">Virtual Server</span> and bind it
                    to one or more resource groups. Common use cases: trim a large MCP
                    server to the few tools a model needs (smaller context), rename tools
                    to match a domain-specific vocabulary, or unify several MCP servers
                    (e.g. Jira + GitHub + Linear) behind one endpoint with a single tool
                    taxonomy.
                </p>
            </InfoBanner>

            {hasErrors && (
                <div className="mb-6">
                    <ErrorDisplay
                        errors={componentErrors}
                        onDismiss={dismissError}
                        onRetry={asyncHandler(handleRetryError, 'retry')}
                    />
                </div>
            )}

            {availableServers.length === 0 && (
                <InfoBanner variant="warning" title="No MCP Servers Available">
                    <p>
                        You need to add and configure MCP servers before creating virtual servers. Go to the Servers tab to get started.
                    </p>
                </InfoBanner>
            )}

            <div className="bg-surface rounded-lg border p-4 mb-6">
                <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4">
                    <div className="flex-1 relative min-h-0 min-w-0">
                        <Search className="w-4 h-4 absolute left-3 top-1/2 transform -translate-y-1/2 text-muted" />
                        <input
                            type="text"
                            placeholder="Search virtual servers by name, ID, description, or owner..."
                            value={searchTerm}
                            onChange={(e) => setSearchTerm(e.target.value)}
                            className="w-full pl-9 pr-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm"
                        />
                    </div>

                    <div className="flex items-center space-x-3">
                        <select
                            value={filterStatus}
                            onChange={(e) => setFilterStatus(e.target.value as any)}
                            className="px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm"
                        >
                            <option value="all">All Status</option>
                            <option value="enabled">Enabled</option>
                            <option value="disabled">Disabled</option>
                        </select>

                        <select
                            value={filterOwner}
                            onChange={(e) => setFilterOwner(e.target.value as any)}
                            className="px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm"
                        >
                            <option value="all">All Owners</option>
                            <option value="mine">My Servers</option>
                            <option value="others">Others' Servers</option>
                        </select>
                    </div>
                </div>

                {hasErrors && componentErrors && componentErrors.length > 0 && (
                    <div className="flex items-center justify-between mt-4 pt-4 border-t">
                        <span className="text-sm text-secondary">
                            {componentErrors.length} error{componentErrors.length !== 1 ? 's' : ''} in virtual server operations
                        </span>
                        <button
                            onClick={() => clearErrors()}
                            className="text-red-600 hover:text-red-800 text-sm underline transition-colors"
                        >
                            Clear All Errors
                        </button>
                    </div>
                )}
            </div>

            {filteredServers.length === 0 ? (
                <div className="bg-surface rounded-lg border overflow-hidden">
                    <div className="text-center py-12 px-4">
                        <Network className="w-16 h-16 text-muted mx-auto mb-4" />
                        <h3 className="text-lg font-medium text-default mb-2">
                            {virtualServers.length === 0 ? 'No virtual servers configured' : 'No virtual servers match your filters'}
                        </h3>
                        <p className="text-secondary mb-6 max-w-md mx-auto">
                            {virtualServers.length === 0
                                ? 'Virtual servers combine capabilities from multiple MCP servers into unified endpoints.'
                                : 'Try adjusting your search or filter criteria to find what you\'re looking for.'}
                        </p>
                        {virtualServers.length === 0 && (
                            <button
                                onClick={() => setShowAddServer(true)}
                                disabled={availableServers.length === 0 || loading}
                                className="px-4 py-2 bg-primary-500 hover:bg-primary-600 text-white rounded-md transition-colors flex items-center space-x-2 mx-auto disabled:opacity-50 disabled:cursor-not-allowed"
                                title={availableServers.length === 0 ? "No MCP servers available to create virtual servers" : "Create Virtual Server"}
                            >
                                <Plus className="w-4 h-4" />
                                <span>Add First Virtual Server</span>
                            </button>
                        )}
                    </div>
                </div>
            ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                    {filteredServers.map((vs) => {
                        const sourceServerIds = getSourceServerIds(vs.capabilities);
                        const totalCapabilities = getTotalCapabilities(vs.capabilities);

                        return (
                            <div
                                key={vs.id}
                                className="bg-surface rounded-lg border shadow-sm hover:shadow-md transition-shadow overflow-hidden"
                            >
                                {/* Card Header — use the themed `bg-primary-50` token
                                    rather than a Tailwind `from-blue-50` gradient. The
                                    latter has no dark-theme CSS variable mapping, so on
                                    dark theme it stayed paper-white while the rest of
                                    the card flipped. `bg-primary-50` is remapped to a
                                    translucent primary tint on dark theme (see
                                    styles/globals.css). Same reasoning for `bg-blue-100`
                                    on the icon square below. */}
                                <div className="p-4 border-b border-default bg-primary-50">
                                    <div className="flex items-start justify-between mb-2">
                                        <div className="flex items-center space-x-3">
                                            <div className="flex-shrink-0 h-12 w-12 bg-primary-100 rounded-lg flex items-center justify-center">
                                                <Network className="h-6 w-6 text-primary-600" />
                                            </div>
                                            <div className="flex-1 min-w-0 min-h-0">
                                                <h3 className="text-lg font-semibold text-default truncate">
                                                    {vs.name}
                                                </h3>
                                                <p className="text-sm text-muted">{vs.id}</p>
                                            </div>
                                        </div>
                                        <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                                            vs.enabled
                                                ? 'bg-green-100 text-green-800'
                                                : 'bg-red-100 text-red-800'
                                        }`}>
                                            {vs.enabled ? 'Enabled' : 'Disabled'}
                                        </span>
                                    </div>
                                    {vs.description && (
                                        <p className="text-sm text-secondary line-clamp-2">
                                            {vs.description}
                                        </p>
                                    )}
                                </div>

                                {/* Capabilities Summary */}
                                <div className="p-4 bg-surface-alt border-b">
                                    <div className="flex items-center justify-between mb-3">
                                        <h4 className="text-sm font-medium text-secondary">Capabilities</h4>
                                        <span className="text-sm font-semibold text-default">
                                            {totalCapabilities} total
                                        </span>
                                    </div>
                                    <div className="grid grid-cols-3 gap-2 mb-3">
                                        {(['tools', 'resources', 'prompts'] as const).map(type => {
                                            const count = vs.capabilities?.[type]?.length || 0;
                                            return (
                                                <div key={type} className="text-center p-2 bg-surface rounded border">
                                                    <div className={`inline-flex items-center justify-center w-8 h-8 rounded-full mb-1 ${getCapabilityColor(type)}`}>
                                                        {getCapabilityIcon(type)}
                                                    </div>
                                                    <div className="text-lg font-semibold text-default">{count}</div>
                                                    <div className="text-xs text-secondary capitalize">{type}</div>
                                                </div>
                                            );
                                        })}
                                    </div>

                                    {/* Capability Details.
                                        Iterate the THREE known capability arrays explicitly
                                        — Object.entries would also yield the normalizer's
                                        non-array siblings (source_mapping, reverse_name_mapping,
                                        tool_name_prefix) and crash on .slice. */}
                                    <div className="space-y-2">
                                        {(['tools', 'resources', 'prompts'] as const).map((type) => {
                                            const items = vs.capabilities?.[type];
                                            if (!Array.isArray(items) || items.length === 0) return null;
                                            return (
                                                <div key={type} className="bg-surface rounded p-2 border">
                                                    <div className="flex items-center space-x-2 mb-1">
                                                        <span className={`inline-flex items-center space-x-1 px-2 py-0.5 rounded text-xs font-medium ${getCapabilityColor(type)}`}>
                                                            {getCapabilityIcon(type)}
                                                            <span className="capitalize">{type}</span>
                                                        </span>
                                                        <span className="text-xs text-muted">({items.length})</span>
                                                    </div>
                                                    <div className="space-y-1 ml-6">
                                                        {items.slice(0, 2).map((cap: any, idx: number) => (
                                                            <div key={idx} className="text-xs text-secondary truncate">
                                                                • {cap.capability_name}
                                                                <span className="text-muted ml-1">
                                                                    from {getServerName(cap.server_id)}
                                                                </span>
                                                            </div>
                                                        ))}
                                                        {items.length > 2 && (
                                                            <div className="text-xs text-blue-600 font-medium">
                                                                +{items.length - 2} more
                                                            </div>
                                                        )}
                                                    </div>
                                                </div>
                                            );
                                        })}
                                    </div>
                                </div>

                                {/* Source Servers */}
                                <div className="p-4 border-b">
                                    <h4 className="text-sm font-medium text-secondary mb-2">Source Servers</h4>
                                    <div className="flex flex-wrap gap-1">
                                        {sourceServerIds.map((serverId) => (
                                            <span key={serverId} className="inline-flex items-center px-2 py-1 rounded text-xs bg-surface-raised text-secondary font-medium">
                                                {getServerName(serverId)}
                                            </span>
                                        ))}
                                    </div>
                                </div>

                                {/* Owner Info */}
                                <div className="p-4 border-b">
                                    <div className="flex items-center justify-between">
                                        <div>
                                            <div className="flex items-center space-x-2">
                                                <User className="w-4 h-4 text-muted" />
                                                <span className="text-sm text-default">
                                                    {vs.owner_display_name}
                                                </span>
                                                {vs.owner === currentUser?.id && (
                                                    <span className="text-xs text-blue-600 font-medium">(You)</span>
                                                )}
                                            </div>
                                            {vs.owner_roles && (
                                                <div className="mt-1 ml-6">
                                                    {getRoleBadge(vs.owner_roles)}
                                                </div>
                                            )}
                                        </div>
                                        <div className="flex items-center space-x-1 text-xs text-muted">
                                            <Calendar className="w-3 h-3" />
                                            <span>{formatDate(vs.created_at)}</span>
                                        </div>
                                    </div>
                                </div>

                                {/* Actions */}
                                <div className="p-4 bg-surface-alt flex items-center justify-end space-x-2">
                                    {canUpdate && (
                                        <>
                                            <button
                                                onClick={asyncHandler(() => vs.id && handleToggleServer(vs.id), 'click')}
                                                className={`p-2 rounded-md transition-colors ${
                                                    vs.enabled
                                                        ? 'text-red-600 hover:bg-red-50'
                                                        : 'text-green-600 hover:bg-green-50'
                                                }`}
                                                title={vs.enabled ? 'Disable' : 'Enable'}
                                            >
                                                {vs.enabled ? <PowerOff className="w-4 h-4" /> : <Power className="w-4 h-4" />}
                                            </button>
                                            <button
                                                onClick={() => setEditingServer(vs)}
                                                className="p-2 text-blue-600 hover:bg-blue-50 rounded-md transition-colors"
                                                title="Edit"
                                            >
                                                <Edit className="w-4 h-4" />
                                            </button>
                                        </>
                                    )}
                                    {canDelete && (
                                            <button
                                                onClick={asyncHandler(() => vs.id && handleDeleteServer(vs.id), 'click')}
                                                className="p-2 text-red-600 hover:bg-red-50 rounded-md transition-colors"
                                                title="Delete"
                                            >
                                                <Trash2 className="w-4 h-4" />
                                            </button>
                                    )}
                                    {!canUpdate && !canDelete && (
                                        <div className="text-xs text-muted italic px-2">
                                            View only
                                        </div>
                                    )}
                                </div>
                            </div>
                        );
                    })}
                </div>
            )}

            {showAddServer && (
                <VirtualServerForm
                    virtualServer={null}
                    availableServers={availableServers}
                    onSave={handleSaveServer}
                    onCancel={() => setShowAddServer(false)}
                />
            )}

            {editingServer && (
                <VirtualServerForm
                    virtualServer={editingServer}
                    availableServers={availableServers}
                    onSave={handleSaveServer}
                    onCancel={() => setEditingServer(null)}
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
                onConfirm={asyncHandler(confirmDeleteServer, 'confirm')}
                itemType="virtual-server"
                itemName={deleteConfirmation?.serverName || ''}
                itemInfo={deleteConfirmation?.serverInfo}
                isDeleting={isDeleting}
                errorMessage={deleteError}
            />

            {/* VMCP tool-override poisoning warning.
                Same modal shape + chip palette as ServersTab's
                upstream-MCP version; copy is VMCP-specific so the
                operator knows the offending text was their own
                override, not an external upstream's metadata. */}
            {poisonedToolsAlert && (
                <div
                    className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4"
                    onMouseDown={(e) => {
                        if (e.target === e.currentTarget) setPoisonedToolsAlert(null);
                    }}
                >
                    <div
                        className="bg-surface rounded-lg shadow-xl w-full max-w-2xl max-h-[90vh] flex flex-col"
                        onMouseDown={(e) => e.stopPropagation()}
                    >
                        <div className="flex items-center justify-between px-5 py-3 border-b border-default flex-shrink-0 bg-red-50">
                            <div className="flex items-center gap-3">
                                <ShieldAlert className="w-6 h-6 text-red-600" />
                                <div>
                                    <h2 className="text-base font-semibold text-red-800">
                                        VMCP override poisoning detected
                                    </h2>
                                    <p className="text-xs text-red-700 mt-0.5">
                                        {poisonedToolsAlert.serverName} — {poisonedToolsAlert.findings.length} operator-supplied override field{poisonedToolsAlert.findings.length === 1 ? '' : 's'} tripped the prompt-injection detector
                                    </p>
                                </div>
                            </div>
                            <button
                                onClick={() => setPoisonedToolsAlert(null)}
                                className="p-2 text-muted hover:text-default hover:bg-surface-alt rounded-md transition-colors"
                                aria-label="Dismiss"
                            >
                                <X className="w-5 h-5" />
                            </button>
                        </div>

                        <div className="scroll-region p-5 space-y-3">
                            <p className="text-sm text-secondary">
                                The virtual MCP server was saved, but one or more of the tool overrides you supplied carry text the prompt-injection detector flagged. The catalog reaches every client model that consumes this VMCP — review each finding and either edit the override or accept the risk. (Inherited upstream descriptions are covered separately by the upstream server's own scan; this view only shows overrides you wrote yourself.)
                            </p>
                            {poisonedToolsAlert.findings.map((f, i) => {
                                const fieldKind = f.field_kind || 'description';
                                const chipClass =
                                    fieldKind === 'name'
                                        ? 'bg-red-100 text-red-800 border-red-200'
                                        : fieldKind === 'title'
                                            ? 'bg-orange-100 text-orange-800 border-orange-200'
                                            : 'bg-amber-100 text-amber-800 border-amber-200';
                                const fieldLabel =
                                    fieldKind === 'name' ? 'exposed_name'
                                        : fieldKind === 'description' ? 'description_override'
                                        : fieldKind;
                                return (
                                <div
                                    key={`${f.tool_name}-${fieldKind}-${i}`}
                                    className="border border-red-200 rounded-lg p-3 bg-red-50/40"
                                >
                                    <div className="flex items-center justify-between mb-2 gap-2 flex-wrap">
                                        <div className="flex items-center gap-2">
                                            <span className="font-mono text-sm font-semibold text-red-800">
                                                {f.tool_name}
                                            </span>
                                            <span className={`text-[10px] font-medium uppercase px-1.5 py-0.5 rounded border ${chipClass}`}>
                                                {fieldLabel}
                                            </span>
                                        </div>
                                        <div className="text-xs text-muted">
                                            confidence {(f.confidence).toFixed(2)} · {f.patterns_matched} pattern{f.patterns_matched === 1 ? '' : 's'} matched · {f.suspicious_indicator_count} indicator{f.suspicious_indicator_count === 1 ? '' : 's'}
                                        </div>
                                    </div>
                                    <pre className="text-xs font-mono whitespace-pre-wrap break-words bg-surface border border-default rounded p-2 max-h-32 overflow-y-auto">
                                        {f.description_excerpt}{f.description_truncated ? '\n…' : ''}
                                    </pre>
                                </div>
                                );
                            })}
                            <p className="text-xs text-muted">
                                These findings are also written as <strong>system_alert</strong> rows in Mission Control (resource_type: <code>virtual_server</code>, error_code: <code>mcp_tool_description_poisoned</code>) — one row per (vmcp, tool, field), deduplicated across re-saves.
                            </p>
                        </div>

                        <div className="flex items-center justify-end gap-2 px-5 py-3 border-t border-default flex-shrink-0 bg-surface-alt">
                            <button
                                onClick={() => setPoisonedToolsAlert(null)}
                                className="text-xs font-medium px-4 py-1.5 rounded-md bg-primary-600 text-white hover:bg-primary-700 transition-colors"
                            >
                                Acknowledge
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
};

export default VirtualServersTab;
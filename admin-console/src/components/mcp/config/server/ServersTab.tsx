/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import { TABLE_TH_TYPE } from '../../../ui/tableStyles';
import {
    Plus,
    Search,
    RefreshCw,
    Activity,
    ShieldAlert,
    X,
} from 'lucide-react';
import MCPIcon from '../../../common/MCPIcon';

import { env } from '../../../../env.ts';

import { MCPServerConfig } from '../../../../lib/types.ts';
import { ErrorDisplay } from '../../../common/ErrorDisplay.tsx';
import { useGateway } from '../../../../hooks/useGateway.ts';
import { useToast } from '../../../common/Toast.tsx';
import ServerForm from './ServerForm.tsx';
import ServerRow from './ServerRow.tsx';
import ProxyUrlGenerator from './ProxyUrlGenerator.tsx';
import OAuthLoginModal from './oauth/OAuthLoginModal.tsx';
import ServerDeletionConflictModal from './ServerDeletionConflictModal.tsx';
import GenericDeleteConfirmationModal from '../../../common/GenericDeleteConfirmationModal.tsx';
import { useMCPServerHealthCheck } from '../../../../hooks/useMCPServerHealthCheck.ts';
import { errorKind, errorMessage, errorStatus } from '../../../../lib/narrow';
import { inUseRefusalOf, type InUseRefusal } from '../../../../lib/inUseRefusal';
import { asyncHandler, runBackground } from '../../../../lib/asyncSafe';
import ReadFailureNotice from '../../../common/ReadFailureNotice';
import { serverHealthReading, type ServerHealthReading } from '../../../../lib/serverHealth';

type HealthFilter = 'all' | ServerHealthReading['state'];
/** The choices of the health filter: every reading of a server's health. */
const HEALTH_FILTERS: { value: HealthFilter; label: string }[] = [
    { value: 'all', label: 'All Health' },
    { value: 'healthy', label: 'Healthy' },
    { value: 'degraded', label: 'Degraded' },
    { value: 'unhealthy', label: 'Unhealthy' },
    { value: 'unknown', label: 'Unknown' },
    { value: 'unrecognised', label: 'Unknown status' },
];


interface ServersTabProps {
    /**
     * Shows the virtual servers. Given by the page that holds both tabs; a
     * refused delete whose answer lists the virtual servers that use the
     * server offers it. Without it the refusal offers no such action.
     */
    onNavigateToVirtualServers?: () => void;
}

const ServersTab: React.FC<ServersTabProps> = ({ onNavigateToVirtualServers }) => {
    const toast = useToast();

    const [editingServer, setEditingServer] = useState<MCPServerConfig | null>(null);
    const [showAddServer, setShowAddServer] = useState(false);
    // Populated when the create endpoint returns
    // `poisoned_tools` (registration-time prompt-injection scan hit
    // one or more tool descriptions). Showing a modal at click
    // time gives the operator immediate visibility instead of
    // requiring them to discover the async Mission Control alert.
    const [poisonedToolsAlert, setPoisonedToolsAlert] = useState<{
        serverName: string;
        findings: Array<{
            tool_name: string;
            // Which metadata field tripped: "description" |
            // "name" | "title".
            field_kind: string;
            description_excerpt: string;
            description_truncated: boolean;
            confidence: number;
            patterns_matched: number;
            suspicious_indicator_count: number;
        }>;
    } | null>(null);
    const [searchTerm, setSearchTerm] = useState('');
    const [filterStatus, setFilterStatus] = useState<'all' | 'enabled' | 'disabled'>('all');
    const [filterAuth, setFilterAuth] = useState<'all' | 'none' | 'bearer' | 'oauth'>('all');
    const [filterCapabilities, setFilterCapabilities] = useState<'all' | 'discovered' | 'none'>('all');
    const [filterHealth, setFilterHealth] = useState<HealthFilter>('all');
    const [selectedServers, setSelectedServers] = useState<Set<string>>(new Set());
    const [showProxyInfo, setShowProxyInfo] = useState<string | null>(null);

    // OAuth info modal state
    const [showOAuthInfo, setShowOAuthInfo] = useState(false);
    const [oauthInfoServer, setOAuthInfoServer] = useState<MCPServerConfig | null>(null);

    // Health check config modal state

    // Delete confirmation modal state
    const [deleteConfirmation, setDeleteConfirmation] = useState<{
        serverId: string;
        serverName: string;
        serverInfo: any;
    } | null>(null);
    const [isDeleting, setIsDeleting] = useState(false);
    const [deleteError, setDeleteError] = useState<string | null>(null);

    // Local state for servers data
    const [servers, setServers] = useState<MCPServerConfig[]>([]);
    const [loading, setLoading] = useState(true);
    const [, setLastFetch] = useState<Date | null>(null);

    // Force component update when lastCheckTime changes
    const [timeDisplayKey, setTimeDisplayKey] = useState(0);

    // A delete the Control Plane answered with a conflict (409), what it
    // said, and what uses the server when the answer lists it.
    const [deletionConflict, setDeletionConflict] = useState<{
        serverName: string;
        errorDetail: string;
        inUse: InUseRefusal | null;
    } | null>(null);

    // Use the gateway hook for operations
    const {
        fetchServers,
        createServer,
        updateServer,
        refreshServerCapabilities,
        deleteServer,
        errors,
        dismissError,
        triggerServerHealthCheck
    } = useGateway();

    const {
        isEnabled: healthCheckEnabled,
        intervalSeconds,
        lastCheckTime,
        isChecking: isAutoChecking,
        runHealthCheck: triggerManualCheck,
        healthCheckConfig,  // Get config from hook
        refreshConfig,  // Get refresh function from hook
        checkFailure,
        configFailure,
    } = useMCPServerHealthCheck();

    // Get actual interval from health check config provided by hook
    const actualIntervalSeconds = healthCheckConfig?.mcp_settings?.interval_seconds ?? intervalSeconds;

    // MCP proxy routes (`/v1/proxy/mcp/{server_id}`) live in the
    // Rust proxy (gateway-core, port 8100), not in the Python
    // control plane (gateway-control, port 5050). Show the proxy
    // URL in the "MCP Proxy Endpoint" modal so the copy-pasted
    // value actually works: the Control Plane's address has no such
    // route. The address is the Core Gateway's, as the console is
    // configured with it (`BRUTOR_GATEWAY_PUBLIC_URL`).
    const effectiveGatewayUrl = env.BRUTOR_GATEWAY_PUBLIC_URL;

    // Force re-render when lastCheckTime changes
    useEffect(() => {
        if (lastCheckTime) {
            setTimeDisplayKey(prev => prev + 1);
        }
    }, [lastCheckTime]);

    // Reload servers after health checks complete (both automatic and manual)
    useEffect(() => {
        if (lastCheckTime) {
            runBackground(loadServers(), 'servers');
        }
    }, [lastCheckTime]);

    // Load servers data. Failure is handled here (the list empties), so the
    // result says whether it worked for the caller that must report it.
    const loadServers = async (): Promise<boolean> => {
        setLoading(true);
        try {
            const serversData = await fetchServers();
            setServers(serversData || []);
            setLastFetch(new Date());
            return true;
        } catch (error) {
            console.error('✗ Failed to load servers:', errorKind(error));
            setServers([]);
            return false;
        } finally {
            setLoading(false);
        }
    };

    // Initial load
    useEffect(() => {
        runBackground(loadServers(), 'servers');
    }, []);

    // Filter to show only server-related errors
    const serverErrors = errors.filter(error =>
        error.context?.resource === 'mcp_server' ||
        error.context?.resource === 'mcp_servers' ||
        error.context?.operation?.includes('server')
    );

    // Filter servers
    const filteredServers = (servers || []).filter(server => {
        const matchesSearch = !searchTerm ||
            server.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
            server.id.toLowerCase().includes(searchTerm.toLowerCase()) ||
            (server.description ?? '').toLowerCase().includes(searchTerm.toLowerCase()) ||
            server.base_url.toLowerCase().includes(searchTerm.toLowerCase());

        const matchesStatus = filterStatus === 'all' ||
            (filterStatus === 'enabled' && server.enabled) ||
            (filterStatus === 'disabled' && !server.enabled);

        const matchesAuth = filterAuth === 'all' || server.auth_type === filterAuth;

        const matchesCapabilities = filterCapabilities === 'all' ||
            (filterCapabilities === 'discovered' && server.capabilities) ||
            (filterCapabilities === 'none' && !server.capabilities);

        // A server never checked reads as unknown; a status no server is
        // stored with has a choice of its own.
        const matchesHealth = filterHealth === 'all' || serverHealthReading(server.health_status).state === filterHealth;

        return matchesSearch && matchesStatus && matchesAuth && matchesCapabilities && matchesHealth;
    });

    // Handle showing OAuth info
    const handleShowOAuthInfo = (serverId: string) => {
        const server = servers.find(s => s.id === serverId);
        if (!server) {
            console.error(`Server not found: ${serverId}`);
            return;
        }

        setOAuthInfoServer(server);
        setShowOAuthInfo(true);
    };

    const handleToggleServer = async (serverId: string) => {
        const server = servers.find(s => s.id === serverId);
        if (!server) return;

        const action = server.enabled ? 'disabled' : 'enabled';

        try {
            await updateServer(serverId, { enabled: !server.enabled });
            await loadServers();
            toast.success(
                `Server ${action.charAt(0).toUpperCase() + action.slice(1)}`,
                `${server.name} has been ${action}.`
            );
        } catch (err) {
            console.error('Failed to toggle server:', errorKind(err));
            toast.error(
                'Failed to Toggle Server',
                errorMessage(err, `Could not ${server.enabled ? 'disable' : 'enable'} ${server.name}.`)
            );
        }
    };

    const handleSaveServer = async (formData: MCPServerConfig) => {
        try {
            let result;

            if (editingServer) {
                result = await updateServer(formData.id, formData);
            } else {
                result = await createServer(formData);
            }

            // Reload servers to get the latest data
            await loadServers();

            // Close the form only after successful save and reload
            if (editingServer) {
                setEditingServer(null);
            } else {
                setShowAddServer(false);
            }

            // The create endpoint returns any tool-
            // description-poisoning findings from the registration-
            // time scan. When present, pop a warning modal at
            // click-time so the operator sees the problem
            // immediately instead of having to find the async
            // Mission Control alert.
            const findings = (result)?.poisoned_tools;
            if (Array.isArray(findings) && findings.length > 0) {
                setPoisonedToolsAlert({
                    serverName: formData.name || 'New MCP server',
                    findings,
                });
            }

            return result;
        } catch (err) {
            console.error('Failed to save server:', errorKind(err));
            throw err;
        }
    };

    const handleDeleteServer = async (serverId: string) => {
        const server = servers.find(s => s.id === serverId);
        if (!server) {
            toast.error('Server Not Found', 'Unable to locate the server to delete.');
            return;
        }

        // Show delete confirmation modal
        setDeleteError(null);
        setDeleteConfirmation({
            serverId: server.id,
            serverName: server.name,
            serverInfo: {
                id: server.id,
                description: server.description,
                enabled: server.enabled
            }
        });
    };

    const confirmDeleteServer = async () => {
        if (!deleteConfirmation) return;

        const { serverId, serverName } = deleteConfirmation;
        const server = servers.find(s => s.id === serverId);
        if (!server) return;

        setIsDeleting(true);
        setLoading(true);

        try {
            setServers(prevServers => prevServers.filter(s => s.id !== serverId));
            setSelectedServers(prev => {
                const newSet = new Set(prev);
                newSet.delete(serverId);
                return newSet;
            });

            await deleteServer(serverId);
            const freshServers = await fetchServers();
            setServers(freshServers);
            setLastFetch(new Date());

            toast.success('Server Deleted', `${serverName} has been deleted successfully.`);

            // Close modal
            setDeleteConfirmation(null);
        } catch (err) {
            console.error('✗ Failed to delete server:', errorKind(err));

            // What the Control Plane answered, as it words it: a conflict
            // (409) in the conflict dialog, anything else in the
            // confirmation it was asked from.
            const said = errorMessage(err, 'The Control Plane gave no reason.');
            if (errorStatus(err) === 409) {
                setDeletionConflict({ serverName, errorDetail: said, inUse: inUseRefusalOf(err) });
                toast.warning('Cannot Delete Server', said);
                // Close delete confirmation modal
                setDeleteConfirmation(null);
            } else {
                setDeleteError(said);
            }

            // Reload to restore UI state
            try {
                const freshServers = await fetchServers();
                setServers(freshServers);
                setLastFetch(new Date());
            } catch (reloadErr) {
                console.error('Failed to reload servers:', errorKind(reloadErr));
            }
        } finally {
            setIsDeleting(false);
            setLoading(false);
        }
    };

    const handleBulkToggle = async (enable: boolean) => {
        if (selectedServers.size === 0) {
            toast.warning('No Servers Selected', 'Please select at least one server to perform bulk operations.');
            return;
        }

        const count = selectedServers.size;

        try {
            await Promise.all(
                Array.from(selectedServers).map(serverId =>
                    updateServer(serverId, { enabled: enable })
                )
            );
            setSelectedServers(new Set());
            await loadServers();
            toast.success(
                `Servers ${enable ? 'Enabled' : 'Disabled'}`,
                `Successfully ${enable ? 'enabled' : 'disabled'} ${count} server${count > 1 ? 's' : ''}.`
            );
        } catch (err) {
            console.error('Failed to update servers:', errorKind(err));
            toast.error(
                `Failed to ${enable ? 'Enable' : 'Disable'} Servers`,
                errorMessage(err, 'An error occurred during the bulk operation.')
            );
        }
    };

    const handleSelectServer = (serverId: string, selected: boolean) => {
        setSelectedServers(prev => {
            const newSet = new Set(prev);
            if (selected) {
                newSet.add(serverId);
            } else {
                newSet.delete(serverId);
            }
            return newSet;
        });
    };

    const handleSelectAll = (selected: boolean) => {
        if (selected) {
            setSelectedServers(new Set(filteredServers.map(s => s.id)));
        } else {
            setSelectedServers(new Set());
        }
    };

    const handleRetryServerError = async (errorId: string) => {
        const error = serverErrors.find(e => e.id === errorId);
        if (!error) return;

        try {
            switch (error.context?.operation) {
                case 'fetch_servers':
                    if (await loadServers()) {
                        toast.success('Retry Successful', 'Servers have been reloaded.');
                    } else {
                        toast.error('Retry Failed', 'The retry operation failed. Please try again.');
                    }
                    break;
                default:
                    toast.info('Manual Retry Required', 'This operation cannot be automatically retried.');
                    break;
            }
        } catch (retryError) {
            console.error('Retry failed:', errorKind(retryError));
            toast.error('Retry Failed', errorMessage(retryError, 'The retry operation failed. Please try again.'));
        }
    };

    const handleServerHealthCheck = async (serverId: string) => {
        const server = servers.find(s => s.id === serverId);
        try {
            await triggerServerHealthCheck(serverId);
            await loadServers();
            toast.success('Health Check Complete', `Health check completed for ${server?.name || serverId}.`);
        } catch (error) {
            console.error(`Health check failed for server ${serverId}:`, errorKind(error));
            toast.error('Health Check Failed', errorMessage(error, `Failed to check health for ${server?.name || serverId}.`));
        }
    };

    const handleRefreshCapabilities = async (serverId: string) => {
        const server = servers.find(s => s.id === serverId);
        const label = server?.name || serverId;
        try {
            const result = await refreshServerCapabilities(serverId);
            await loadServers();

            if (!result.discovered) {
                // OAuth / disabled — surface the skip reason without a red error.
                toast.info('Capability Refresh Skipped', result.message || `${label}: discovery was not run.`);
                return;
            }

            const title = result.drift_detected
                ? 'Capabilities Refreshed (drift detected)'
                : 'Capabilities Refreshed';
            toast.success(title, `${label}: ${result.message || `${result.tools_count} tools`}.`);

            // Poison findings are surfaced as Mission Control alerts
            // asynchronously; nudge the operator inline if any tripped.
            if (result.poisoned_tools && result.poisoned_tools.length > 0) {
                toast.warning(
                    'Tool description flagged',
                    `${result.poisoned_tools.length} tool description(s) tripped the prompt-injection scanner. Review in Mission Control.`
                );
            }
        } catch (error) {
            console.error(`Capability refresh failed for server ${serverId}:`, errorKind(error));
            toast.error('Capability Refresh Failed', errorMessage(error, `Failed to refresh capabilities for ${label}.`));
        }
    };

    const handleIconChange = async () => {
        try {
            await loadServers();

            if (editingServer) {
                const serversData = await fetchServers();
                const updatedServer = serversData.find(s => s.id === editingServer.id);
                if (updatedServer) {
                    setEditingServer(updatedServer);
                }
            }
        } catch (error) {
            console.error('Failed to refresh after icon change:', errorKind(error));
        }
    };

    if (loading && servers.length === 0) {
        return (
            <div className="p-6">
                <div className="flex justify-between items-center mb-6">
                    <div>
                        <h1 className="text-2xl font-bold text-default mb-2">MCP Servers</h1>
                        <p className="text-secondary">Loading server configurations...</p>
                    </div>
                </div>
                <div className="flex items-center justify-center h-64">
                    <div className="text-center">
                        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-primary mx-auto mb-4"></div>
                        <p className="text-secondary">Loading servers...</p>
                    </div>
                </div>
            </div>
        );
    }

    return (
        <div className="p-6">
            <div className="flex justify-between items-center mb-6">
                <div>
                    <h1 className="text-2xl font-bold text-default mb-2">MCP Servers</h1>
                    <p className="text-secondary">
                        Manage registered MCP servers ({filteredServers.length} of {servers.length})
                    </p>
                </div>
                <div className="flex items-center space-x-3">
                    <ReadFailureNotice compact failure={checkFailure} onRetry={asyncHandler(triggerManualCheck, 'health check')} />
                    <ReadFailureNotice compact failure={configFailure} onRetry={asyncHandler(refreshConfig, 'health check settings', 'background')} />
                    {/* Health Check Banner - Inline */}
                    {(healthCheckEnabled || lastCheckTime) && (
                        <div className="flex items-center space-x-2 px-3 py-2 bg-blue-50 border border-blue-200 rounded-md">
                            {healthCheckEnabled ? (
                                <>
                                    <Activity className={`w-4 h-4 text-green-600 ${isAutoChecking ? 'animate-pulse' : ''}`} />
                                    <span className="text-sm text-secondary">
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
                                    <span className="text-sm text-secondary">Health check disabled</span>
                                </>
                            )}
                            <button
                                onClick={asyncHandler(async () => {
                                    await triggerManualCheck();
                                    // Server reload happens automatically via lastCheckTime effect
                                }, 'click')}
                                disabled={isAutoChecking}
                                className="flex items-center space-x-1 px-2 py-1 text-xs bg-surface border border-strong rounded hover:bg-surface-alt transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                                title="Run health checks now"
                            >
                                <RefreshCw className={`w-3 h-3 ${isAutoChecking ? 'animate-spin' : ''}`} />
                                <span>{isAutoChecking ? 'Checking...' : 'Check Now'}</span>
                            </button>
                        </div>
                    )}
                    <button
                        onClick={asyncHandler(loadServers, 'click')}
                        disabled={loading}
                        className="flex items-center space-x-2 px-3 py-2 text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt disabled:opacity-50"
                    >
                        <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
                        <span>Refresh</span>
                    </button>
                    <button
                        onClick={() => setShowAddServer(true)}
                        className="px-4 py-2 bg-primary-500 hover:bg-primary-600 text-white rounded-md transition-colors flex items-center space-x-2"
                        disabled={loading}
                    >
                        <Plus className="w-4 h-4" />
                        <span>Add Server</span>
                    </button>
                </div>
            </div>

            {/* Server-specific Error Display */}
            {serverErrors.length > 0 && (
                <div className="mb-6">
                    <ErrorDisplay
                        errors={serverErrors}
                        onDismiss={dismissError}
                        onRetry={asyncHandler(handleRetryServerError, 'retry')}
                    />
                </div>
            )}

            {/* Filters and Search */}
            <div className="bg-surface rounded-lg border p-4 mb-6">
                <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4">
                    <div className="flex-1 relative min-h-0 min-w-0">
                        <Search className="w-4 h-4 absolute left-3 top-1/2 transform -translate-y-1/2 text-muted" />
                        <input
                            type="text"
                            placeholder="Search servers by name, ID, description, or URL..."
                            value={searchTerm}
                            onChange={(e) => setSearchTerm(e.target.value)}
                            className="w-full pl-9 pr-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                        />
                    </div>

                    <div className="flex items-center space-x-3">
                        <select
                            value={filterStatus}
                            onChange={(e) => setFilterStatus(e.target.value as any)}
                            className="px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                        >
                            <option value="all">All Status</option>
                            <option value="enabled">Enabled</option>
                            <option value="disabled">Disabled</option>
                        </select>

                        <select
                            value={filterAuth}
                            onChange={(e) => setFilterAuth(e.target.value as any)}
                            className="px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                        >
                            <option value="all">All Auth Types</option>
                            <option value="none">No Auth</option>
                            <option value="bearer">Bearer Token</option>
                            <option value="oauth">OAuth</option>
                        </select>

                        <select
                            value={filterCapabilities}
                            onChange={(e) => setFilterCapabilities(e.target.value as any)}
                            className="px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                        >
                            <option value="all">All Capabilities</option>
                            <option value="discovered">Discovered</option>
                            <option value="none">Not Discovered</option>
                        </select>

                        {(healthCheckEnabled || lastCheckTime) && (
                            <select
                                value={filterHealth}
                                onChange={(e) => setFilterHealth(HEALTH_FILTERS.find((f) => f.value === e.target.value)?.value ?? 'all')}
                                className="px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                            >
                                {HEALTH_FILTERS.map((f) => (
                                    <option key={f.value} value={f.value}>{f.label}</option>
                                ))}
                            </select>
                        )}
                    </div>
                </div>

                {/* Bulk Actions */}
                {selectedServers.size > 0 && (
                    <div className="flex items-center justify-between mt-4 pt-4 border-t">
                        <span className="text-sm text-secondary">
                            {selectedServers.size} server{selectedServers.size !== 1 ? 's' : ''} selected
                        </span>
                        <div className="flex items-center space-x-2">
                            <button
                                onClick={asyncHandler(() => handleBulkToggle(true), 'click')}
                                disabled={loading}
                                className="px-4 py-2 bg-primary-500 hover:bg-primary-600 text-white rounded-md transition-colors flex items-center space-x-2"
                            >
                                Enable Selected
                            </button>
                            <button
                                onClick={asyncHandler(() => handleBulkToggle(false), 'click')}
                                disabled={loading}
                                className="px-4 py-2 bg-primary-500 hover:bg-primary-600 text-white rounded-md transition-colors flex items-center space-x-2"
                            >
                                Disable Selected
                            </button>
                        </div>
                    </div>
                )}
            </div>

            {/* Servers Table */}
            <div className="bg-surface rounded-lg border">
                {filteredServers.length === 0 ? (
                    <div className="text-center py-12">
                        <MCPIcon className="w-12 h-12 text-muted mx-auto mb-4" />
                        <h3 className="text-lg font-medium text-default mb-2">
                            {servers.length === 0 ? 'No servers configured' : 'No servers match your filters'}
                        </h3>
                        <p className="text-secondary mb-4">
                            {servers.length === 0
                                ? 'Get started by adding your first MCP server.'
                                : 'Try adjusting your search or filter criteria.'}
                        </p>
                        {servers.length === 0 && (
                            <button
                                onClick={() => setShowAddServer(true)}
                                className="px-4 py-2 bg-primary-500 hover:bg-primary-600 text-white rounded-md transition-colors flex items-center space-x-2 mx-auto"
                            >
                                <Plus className="w-4 h-4" />
                                <span>Add First Server</span>
                            </button>
                        )}
                    </div>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="min-w-full divide-y divide-default">
                            <thead className="bg-surface-alt">
                            <tr>
                                <th className="px-6 py-3 text-left">
                                    <input
                                        type="checkbox"
                                        checked={filteredServers.length > 0 && selectedServers.size === filteredServers.length}
                                        onChange={(e) => handleSelectAll(e.target.checked)}
                                        className="rounded border-strong"
                                    />
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Server
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Status
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    MCP Endpoint
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Auth Type
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Capabilities
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-right`}>
                                    Actions
                                </th>
                            </tr>
                            </thead>
                            <tbody className="bg-surface divide-y divide-default">
                            {filteredServers.map((server) => (
                                <ServerRow
                                    key={server.id}
                                    server={server}
                                    isSelected={selectedServers.has(server.id)}
                                    loading={loading}
                                    gatewayBaseUrl={effectiveGatewayUrl}
                                    onSelect={(selected) => handleSelectServer(server.id, selected)}
                                    onToggle={asyncHandler(() => handleToggleServer(server.id), 'toggle')}
                                    onEdit={() => setEditingServer(server)}
                                    onDelete={asyncHandler(() => handleDeleteServer(server.id), 'delete')}
                                    onHealthCheck={(healthCheckEnabled || lastCheckTime) ? handleServerHealthCheck : undefined}
                                    onRefreshCapabilities={handleRefreshCapabilities}
                                    onShowOAuthInfo={handleShowOAuthInfo}
                                />
                            ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </div>

            {/* Standalone Proxy URL Generator for selected server */}
            {showProxyInfo && (
                <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
                    <div className="bg-surface rounded-lg shadow-xl max-w-4xl w-full max-h-[90vh] overflow-y-auto">
                        <div className="flex items-center justify-between px-6 py-4 border-b">
                            <h3 className="text-lg font-semibold text-default">
                                MCP Proxy Endpoints
                            </h3>
                            <button
                                onClick={() => setShowProxyInfo(null)}
                                className="p-2 hover:bg-surface-raised rounded-md transition-colors"
                            >
                                <span className="sr-only">Close</span>
                                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                                </svg>
                            </button>
                        </div>
                        <div className="p-6">
                            {(() => {
                                const server = servers.find(s => s.id === showProxyInfo);
                                if (!server) return null;
                                return (
                                    <ProxyUrlGenerator
                                        serverId={server.id}
                                        serverName={server.name}
                                        gatewayBaseUrl={effectiveGatewayUrl}
                                    />
                                );
                            })()}
                        </div>
                    </div>
                </div>
            )}

            {/* Forms */}
            {showAddServer && (
                <ServerForm
                    server={null}
                    onSave={handleSaveServer}
                    onCancel={() => setShowAddServer(false)}
                    onIconChange={handleIconChange}
                />
            )}

            {editingServer && (
                <ServerForm
                    server={editingServer}
                    onSave={handleSaveServer}
                    onCancel={() => setEditingServer(null)}
                    onIconChange={handleIconChange}
                />
            )}

            {/* OAuth Info Modal */}
            {showOAuthInfo && oauthInfoServer && (
                <OAuthLoginModal
                    server={oauthInfoServer}
                    onClose={() => {
                        setShowOAuthInfo(false);
                        setOAuthInfoServer(null);
                    }}
                />
            )}

            {/* Deletion Conflict Modal */}
            <ServerDeletionConflictModal
                isOpen={!!deletionConflict}
                onClose={() => setDeletionConflict(null)}
                serverName={deletionConflict?.serverName || ''}
                errorDetail={deletionConflict?.errorDetail || ''}
                inUse={deletionConflict?.inUse ?? null}
                onNavigateToVirtualServers={onNavigateToVirtualServers}
            />

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
                itemType="server"
                itemName={deleteConfirmation?.serverName || ''}
                itemInfo={deleteConfirmation?.serverInfo}
                isDeleting={isDeleting}
                errorMessage={deleteError}
            />

            {/* Health Check Config Modal */}

            {/* Registration-time poison warning modal.
                Shown immediately after Create when one or more
                tool descriptions tripped the prompt-injection
                detector. Operators see the findings synchronously
                instead of having to discover the async Mission
                Control alert. */}
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
                                        Tool description poisoning detected
                                    </h2>
                                    <p className="text-xs text-red-700 mt-0.5">
                                        {poisonedToolsAlert.serverName} — {poisonedToolsAlert.findings.length} tool{poisonedToolsAlert.findings.length === 1 ? '' : 's'} tripped the prompt-injection detector at registration
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
                                The server was registered, but the tools below carry descriptions that match the prompt-injection detector. A poisoned description reaches the client model verbatim during planning — exactly the surface a tool-description-injection attack targets. Review each finding and decide whether to disable the server, remove the offending tool binding from the resource group, or accept the risk.
                            </p>
                            {poisonedToolsAlert.findings.map((f, i) => {
                                const fieldKind = f.field_kind;
                                // Field-kind chip colour: name = red
                                // (highest signal — the name itself
                                // is operator-controlled label seen
                                // in the catalog), title = orange
                                // (display-only metadata), description
                                // = amber (free-form prose where
                                // false positives are slightly more
                                // common). All still hit the same
                                // detector with the same threshold.
                                const chipClass =
                                    fieldKind === 'name'
                                        ? 'bg-red-100 text-red-800 border-red-200'
                                        : fieldKind === 'title'
                                            ? 'bg-orange-100 text-orange-800 border-orange-200'
                                            : 'bg-amber-100 text-amber-800 border-amber-200';
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
                                                {fieldKind}
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
                                These findings are also written as <strong>system_alert</strong> rows in Mission Control (category: <code>infrastructure_threshold</code>, error_code: <code>mcp_tool_description_poisoned</code>) — one row per (server, tool), deduplicated across re-discoveries.
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

export default ServersTab;
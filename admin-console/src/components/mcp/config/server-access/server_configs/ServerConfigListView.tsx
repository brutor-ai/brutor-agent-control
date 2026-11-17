/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useRef, useCallback } from 'react';
import { TABLE_TH_TYPE } from '../../../../ui/tableStyles';
import { createPortal } from 'react-dom';
import {
    Key,
    Edit,
    Trash2,
    CheckCircle,
    XCircle,
    Shield,
    ShieldOff,
    Link as LinkIcon,
    Layers, // VMCP icon (composed/stacked → composition)
    Info
} from 'lucide-react';
import { ServerConfig } from '../../../../../lib/types.ts';
import { useGateway } from '../../../../../hooks/useGateway.ts';
import GroupTreeDisplay from '../../../../common/GroupTreeDisplay.tsx';

// Server Icon Component
interface ServerIconProps {
    config: ServerConfig;
    getServerIconUrl: (serverId: string) => string;
}

const ServerIcon: React.FC<ServerIconProps> = ({ config, getServerIconUrl }) => {
    const [iconError, setIconError] = useState(false);
    const [imageLoaded, setImageLoaded] = useState(false);

    const getIconUrl = (): string | null => {
        // VMCP-backed configs don't carry an MCP icon
        // (the list view renders a sky-blue Layers badge instead).
        if (!config.mcp_server?.icon?.url || iconError || !config.mcp_server_id) {
            return null;
        }

        const serverId = config.mcp_server_id;
        const baseUrl = getServerIconUrl(serverId);

        const timestamp = config.mcp_server.icon.uploaded_at
            ? new Date(config.mcp_server.icon.uploaded_at).getTime()
            : Date.now();
        const cacheKey = `${timestamp}_${Math.random().toString(36).substr(2, 9)}`;

        return `${baseUrl}?t=${cacheKey}`;
    };

    const iconUrl = getIconUrl();

    if (iconUrl && !iconError) {
        return (
            <>
                <img
                    src={iconUrl}
                    alt={config.mcp_server?.name || config.name}
                    className="w-8 h-8 rounded object-contain border border-default bg-surface-alt"
                    style={{ display: imageLoaded ? 'block' : 'none' }}
                    onError={() => setIconError(true)}
                    onLoad={() => setImageLoaded(true)}
                />
                {!imageLoaded && !iconError && (
                    <div className="w-8 h-8 rounded bg-surface-raised flex items-center justify-center">
                        <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-primary-600"></div>
                    </div>
                )}
            </>
        );
    }

    // Fallback icon
    return (
        <div className="w-8 h-8 rounded bg-primary-100 flex items-center justify-center">
            <LinkIcon className="w-4 h-4 text-primary-600" />
        </div>
    );
};

interface ServerConfigListViewProps {
    configs: ServerConfig[];
    servers: any[];
    // VMCPs the operator can bind. Used to look up
    // the friendly name + render the sky-blue "VMCP" badge on
    // VMCP-backed config rows.
    virtualServers?: any[];
    oauthClients: any[];
    onEdit: (config: ServerConfig) => void;
    onDelete: (configId: string, configName: string) => void;
}

const ServerConfigListView: React.FC<ServerConfigListViewProps> = ({
                                                                       configs,
                                                                       servers,
                                                                       virtualServers = [],
                                                                       oauthClients,
                                                                       onEdit,
                                                                       onDelete
                                                                   }) => {
    const { getServerIconUrl } = useGateway();

    const [showStatusTooltip, setShowStatusTooltip] = useState(false);
    const statusInfoRef = useRef<HTMLButtonElement>(null);
    const [tooltipPos, setTooltipPos] = useState({ top: 0, left: 0 });

    const handleToggleTooltip = useCallback(() => {
        if (!showStatusTooltip && statusInfoRef.current) {
            const rect = statusInfoRef.current.getBoundingClientRect();
            setTooltipPos({
                top: rect.top - 8,
                left: rect.left + rect.width / 2,
            });
        }
        setShowStatusTooltip(prev => !prev);
    }, [showStatusTooltip]);

    const getServerName = (serverId: string): string => {
        const server = servers.find(s => s.id === serverId);
        return server ? server.name : serverId;
    };

    // Friendly-name lookup for VMCP-backed configs.
    const getVirtualServerName = (vserverId: string): string => {
        const v = virtualServers.find((vs: any) => vs.id === vserverId);
        return v ? v.name : vserverId;
    };

    const getOAuthClientName = (clientId: string): string => {
        const client = oauthClients.find(c => c.id === clientId);
        return client ? client.name : clientId;
    };

    const renderGroupsCell = (config: ServerConfig) => {
        return (
            <GroupTreeDisplay
                groups={config.groups || []}
                variant="compact"
                maxVisible={2}
            />
        );
    };

    return (
        <div className="bg-surface rounded-lg border border-default overflow-hidden">
            <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-default">
                    <thead className="bg-surface-alt">
                    <tr>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Configuration
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            MCP / VMCP Server
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Auth
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Access
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Priority
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            <div className="flex items-center space-x-1">
                                <span>Status</span>
                                <button
                                    ref={statusInfoRef}
                                    onClick={handleToggleTooltip}
                                    className="text-muted hover:text-secondary transition-colors"
                                >
                                    <Info className="w-3.5 h-3.5" />
                                </button>
                                {showStatusTooltip && createPortal(
                                    <>
                                        <div className="fixed inset-0 z-[9998]" onClick={() => setShowStatusTooltip(false)} />
                                        <div
                                            className="fixed z-[9999] w-72 p-3 bg-gray-900 text-white text-xs rounded-lg shadow-lg"
                                            style={{ top: tooltipPos.top, left: tooltipPos.left, transform: 'translate(-50%, -100%)' }}
                                        >
                                            <p className="font-semibold mb-1.5">Two-level enable/disable</p>
                                            <p className="mb-1">This status reflects both the <strong>MCP Server</strong> and its <strong>Access Configuration</strong> enabled settings.</p>
                                            <p className="mb-1">If the MCP Server is disabled globally, all its configurations are effectively disabled — shown as <em>"Disabled (by MCP Server)"</em>.</p>
                                            <p>The Access Configuration's own enabled setting only takes effect when the parent MCP Server is enabled.</p>
                                            <div className="absolute top-full left-1/2 -translate-x-1/2 border-4 border-transparent border-t-gray-900" />
                                        </div>
                                    </>,
                                    document.body
                                )}
                            </div>
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-right`}>
                            Actions
                        </th>
                    </tr>
                    </thead>
                    <tbody className="bg-surface divide-y divide-default">
                    {configs.map((config) => {
                        const isOAuthProtected = config.mcp_server?.auth_type === 'oauth2' ||
                            config.mcp_server?.auth_type === 'oauth' ||
                            !!config.oauth_client_id;

                        return (
                            <tr key={config.id} className="hover:bg-surface-alt transition-colors">
                                {/* Configuration Name */}
                                <td className="px-4 py-4 whitespace-nowrap">
                                    <div>
                                        <div className="text-sm font-medium text-default">{config.name}</div>
                                        <div className="text-xs text-muted font-mono" title={config.id}>{config.id}</div>
                                        {config.description && (
                                            <div className="text-xs text-muted truncate max-w-xs">
                                                {config.description}
                                            </div>
                                        )}
                                        <div className="flex items-center gap-1 mt-1">
                                            {config.is_default && (
                                                <span className="inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium bg-primary-100 text-primary-800">
                                                        Default
                                                    </span>
                                            )}
                                            <span className="text-xs text-muted">
                                                    {config.usage_count} uses
                                                </span>
                                        </div>
                                    </div>
                                </td>

                                {/* MCP / VMCP Server.
                                    VMCP-backed configs show a sky-blue
                                    Layers icon + name + "VMCP" badge so
                                    operators see at a glance that this
                                    row delegates to a composition rather
                                    than a single upstream. */}
                                <td className="px-4 py-4 whitespace-nowrap">
                                    {config.server_kind === 'vmcp' || config.virtual_server_id ? (
                                        <div className="flex items-center space-x-2">
                                            <div className="w-8 h-8 rounded flex items-center justify-center bg-sky-100">
                                                <Layers className="w-4 h-4 text-sky-700" />
                                            </div>
                                            <div className="flex flex-col">
                                                <span className="text-sm text-default">
                                                    {config.virtual_server?.name
                                                        || getVirtualServerName(config.virtual_server_id || '')}
                                                </span>
                                                <span
                                                    className="inline-flex items-center w-fit px-1.5 py-0 rounded text-[10px] font-medium bg-sky-100 text-sky-700"
                                                    title="Virtual MCP — composes tools from multiple upstream MCP servers"
                                                >
                                                    VMCP
                                                </span>
                                            </div>
                                        </div>
                                    ) : (
                                        <div className="flex items-center space-x-2">
                                            <ServerIcon
                                                config={config}
                                                getServerIconUrl={getServerIconUrl}
                                            />
                                            <span className="text-sm text-default">
                                                {config.mcp_server?.name
                                                    || (config.mcp_server_id && getServerName(config.mcp_server_id))
                                                    || '—'}
                                            </span>
                                        </div>
                                    )}
                                </td>

                                {/* Authentication */}
                                <td className="px-4 py-4 whitespace-nowrap">
                                    {(config.server_kind === 'vmcp' || config.virtual_server_id) ? (
                                        // VMCP-backed configs
                                        // have no per-config OAuth; the
                                        // VMCP delegates per-call to the
                                        // underlying MCP servers, each of
                                        // which carries its own OAuth
                                        // context.
                                        <div className="flex items-center space-x-1" title="VMCPs delegate per-call auth to the underlying MCP servers; configure OAuth on each underlying MCP's config.">
                                            <Layers className="w-3 h-3 text-sky-600" />
                                            <span className="text-xs text-secondary">Delegated</span>
                                        </div>
                                    ) : isOAuthProtected ? (
                                        <div>
                                            <div className="flex items-center space-x-1 mb-1">
                                                <Shield className="w-3 h-3 text-green-600" />
                                                <span className="text-xs font-medium text-green-700">OAuth</span>
                                            </div>
                                            <div className="flex items-center space-x-1">
                                                <Key className="w-3 h-3 text-muted" />
                                                <span className="text-xs text-secondary">
                                                        {config.oauth_client?.name || (config.oauth_client_id && getOAuthClientName(config.oauth_client_id))}
                                                    </span>
                                            </div>
                                        </div>
                                    ) : (
                                        <div className="flex items-center space-x-1">
                                            <ShieldOff className="w-3 h-3 text-muted" />
                                            <span className="text-xs text-secondary">Open</span>
                                        </div>
                                    )}
                                </td>

                                {/* Access (Groups) */}
                                <td className="px-4 py-4">
                                    {renderGroupsCell(config)}
                                </td>

                                {/* Priority */}
                                <td className="px-4 py-4 whitespace-nowrap">
                                        <span className="text-sm text-default font-medium">
                                            {config.priority}
                                        </span>
                                </td>

                                {/* Status */}
                                <td className="px-4 py-4 whitespace-nowrap">
                                    {(() => {
                                        const effectiveEnabled = config.effective_enabled ?? config.enabled;
                                        const disabledByServer = config.mcp_server?.enabled === false;
                                        if (effectiveEnabled) {
                                            return (
                                                <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-green-100 text-green-800">
                                                    <CheckCircle className="w-3 h-3 mr-1" />
                                                    Enabled
                                                </span>
                                            );
                                        }
                                        return (
                                            <div>
                                                <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-surface-raised text-default">
                                                    <XCircle className="w-3 h-3 mr-1" />
                                                    Disabled
                                                </span>
                                                {disabledByServer && (
                                                    <div className="text-xs text-amber-600 mt-1">(by MCP Server)</div>
                                                )}
                                            </div>
                                        );
                                    })()}
                                </td>

                                {/* Actions */}
                                <td className="px-4 py-4 whitespace-nowrap text-right">
                                    <div className="flex items-center justify-end space-x-1">
                                        <button
                                            onClick={() => onEdit(config)}
                                            className="p-1.5 text-primary hover:text-primary-700 hover:bg-primary-50 rounded transition-colors"
                                            title="Edit"
                                        >
                                            <Edit className="w-4 h-4" />
                                        </button>
                                        <button
                                            onClick={() => onDelete(config.id, config.name)}
                                            className="p-1.5 text-red-600 hover:text-red-700 hover:bg-red-50 rounded transition-colors"
                                            title="Delete"
                                        >
                                            <Trash2 className="w-4 h-4" />
                                        </button>
                                    </div>
                                </td>
                            </tr>
                        );
                    })}
                    </tbody>
                </table>
            </div>

            {/* Footer with count */}
            <div className="px-4 py-3 bg-surface-alt border-t border-default">
                <div className="text-sm text-secondary">
                    Showing <span className="font-medium">{configs.length}</span> configuration{configs.length !== 1 ? 's' : ''}
                </div>
            </div>
        </div>
    );
};

export default ServerConfigListView;
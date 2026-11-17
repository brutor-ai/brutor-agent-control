/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';
import {
    Link as LinkIcon,
    Server,
    Key,
    Edit,
    Trash2,
    CheckCircle,
    XCircle,
    Shield,
    ShieldOff
} from 'lucide-react';
import { useGateway } from '../../../../../hooks/useGateway';
import { ServerConfig } from '../../../../../lib/types';
import GroupTreeDisplay from '../../../../common/GroupTreeDisplay';

interface ServerConfigRowProps {
    config: ServerConfig;
    serverName: string;
    oauthClientName?: string;
    oauthClientAppType?: string;
    onEdit: (config: ServerConfig) => void;
    onDelete: (configId: string, configName: string) => void;
}

const ServerConfigRow: React.FC<ServerConfigRowProps> = ({
                                                             config,
                                                             serverName,
                                                             oauthClientName,
                                                             oauthClientAppType,
                                                             onEdit,
                                                             onDelete
                                                         }) => {
    const [iconError, setIconError] = React.useState(false);
    const [imageLoaded, setImageLoaded] = React.useState(false);

    // FIX: Determine if this is an OAuth-protected server (check for both 'oauth' and 'oauth2')
    const isOAuthProtected = config.mcp_server?.auth_type === 'oauth2' ||
        config.mcp_server?.auth_type === 'oauth' ||
        !!config.oauth_client_id;

    const { getServerIconUrl } = useGateway();

    const getIconUrl = (): string | null => {
        if (iconError) return null;

        // Priority 1: uploaded binary icon on the MCP server.
        // VMCP-backed configs have no mcp_server_id;
        // bail out so the parent component renders the sky-blue
        // VMCP badge instead.
        if (config.mcp_server?.icon?.url && config.mcp_server_id) {
            const serverId = config.mcp_server_id;
            const baseUrl = getServerIconUrl(serverId);

            // Keyed on the upload only: anything per-render here changes the src
            // and re-downloads the icon on every re-render of the list.
            const uploadedAt = config.mcp_server.icon.uploaded_at;
            return uploadedAt ? `${baseUrl}?t=${new Date(uploadedAt).getTime()}` : baseUrl;
        }

        // Priority 2: external icon_url (e.g. from catalog import)
        if (config.icon_url) {
            return config.icon_url;
        }

        return null;
    };

    const serverIconUrl = getIconUrl();

    return (
        <div className="bg-surface rounded-lg border border-default hover:shadow-lg transition-shadow">
            <div className="p-5">
                {/* Header Section */}
                <div className="flex items-start justify-between mb-4">
                    <div className="flex items-start space-x-3 flex-1 min-h-0 min-w-0">
                        {/* Server Icon or Fallback */}
                        <div className="flex-shrink-0 relative">
                            {serverIconUrl && !iconError ? (
                                <>
                                    <img
                                        src={serverIconUrl}
                                        alt={serverName}
                                        className="w-12 h-12 rounded-lg object-contain border border-default bg-surface-alt"
                                        style={{ display: imageLoaded ? 'block' : 'none' }}
                                        onError={() => {
                                            console.error(`Failed to load icon from: ${serverIconUrl}`);
                                            setIconError(true);
                                        }}
                                        onLoad={() => {
                                            setImageLoaded(true);
                                        }}
                                    />
                                    {!imageLoaded && !iconError && (
                                        <div className="w-12 h-12 rounded-lg bg-surface-raised flex items-center justify-center">
                                            <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-primary-600"></div>
                                        </div>
                                    )}
                                </>
                            ) : (
                                <div className="w-12 h-12 rounded-lg bg-primary-100 flex items-center justify-center">
                                    <LinkIcon className="w-6 h-6 text-primary-600" />
                                </div>
                            )}
                        </div>

                        {/* Title and Status Badges */}
                        <div className="flex-1 min-w-0 min-h-0">
                            <div className="flex items-center space-x-2 mb-1 flex-wrap">
                                <h3 className="font-semibold text-default text-lg">{config.name}</h3>

                                {/* OAuth Status Badge - Shows OAuth vs Open */}
                                {isOAuthProtected ? (
                                    <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-green-100 text-green-800">
                                        <Shield className="w-3 h-3 mr-1" />
                                        OAuth
                                    </span>
                                ) : (
                                    <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-surface-raised text-default">
                                        <ShieldOff className="w-3 h-3 mr-1" />
                                        Open
                                    </span>
                                )}

                                {config.is_default && (
                                    <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-primary-100 text-primary-800">
                                        Default
                                    </span>
                                )}
                                {config.enabled ? (
                                    <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-green-100 text-green-800">
                                        <CheckCircle className="w-3 h-3 mr-1" />
                                        Enabled
                                    </span>
                                ) : (
                                    <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-surface-raised text-default">
                                        <XCircle className="w-3 h-3 mr-1" />
                                        Disabled
                                    </span>
                                )}
                            </div>

                            {config.description && (
                                <p className="text-sm text-secondary mb-2">{config.description}</p>
                            )}
                        </div>

                        {/* Action Buttons */}
                        <div className="flex items-center space-x-1 flex-shrink-0">
                            <button
                                onClick={() => onEdit(config)}
                                className="p-2 text-primary hover:text-primary-700 hover:bg-primary-50 rounded transition-colors"
                                title="Edit"
                            >
                                <Edit className="w-4 h-4" />
                            </button>
                            <button
                                onClick={() => onDelete(config.id, config.name)}
                                className="p-2 text-red-600 hover:text-red-700 hover:bg-red-50 rounded transition-colors"
                                title="Delete"
                            >
                                <Trash2 className="w-4 h-4" />
                            </button>
                        </div>
                    </div>
                </div>

                {/* Details Grid */}
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">
                    {/* MCP Server */}
                    <div className="flex items-start space-x-2">
                        <Server className="w-4 h-4 text-muted mt-0.5 flex-shrink-0" />
                        <div className="min-w-0 flex-1 min-h-0">
                            <span className="text-xs text-muted block">MCP Server</span>
                            <span className="text-sm text-default font-medium break-words">{serverName}</span>
                        </div>
                    </div>

                    {/* OAuth Client (only show if OAuth-protected) */}
                    {isOAuthProtected && oauthClientName && (
                        <div className="flex items-start space-x-2">
                            <Key className="w-4 h-4 text-muted mt-0.5 flex-shrink-0" />
                            <div className="min-w-0 flex-1 min-h-0">
                                <span className="text-xs text-muted block">OAuth Client</span>
                                <div className="flex items-center space-x-2">
                                    <span className="text-sm text-default font-medium">{oauthClientName}</span>
                                    {oauthClientAppType && (
                                        <span className="text-xs text-muted px-2 py-0.5 bg-surface-raised rounded">
                                            {oauthClientAppType}
                                        </span>
                                    )}
                                </div>
                            </div>
                        </div>
                    )}

                    {/* Authentication Type (if no OAuth) */}
                    {!isOAuthProtected && (
                        <div className="flex items-start space-x-2">
                            <ShieldOff className="w-4 h-4 text-muted mt-0.5 flex-shrink-0" />
                            <div className="min-w-0 flex-1 min-h-0">
                                <span className="text-xs text-muted block">Authentication</span>
                                <span className="text-sm text-default">None Required</span>
                            </div>
                        </div>
                    )}

                    {/* Priority and Usage */}
                    <div className="flex items-start space-x-2">
                        <div className="w-4 h-4 text-muted mt-0.5 flex-shrink-0 flex items-center justify-center text-xs font-bold">
                            #
                        </div>
                        <div className="min-w-0 flex-1 min-h-0">
                            <span className="text-xs text-muted block">Priority & Usage</span>
                            <div className="text-sm text-default">
                                <span className="font-medium">Priority: {config.priority}</span>
                                <span className="text-muted mx-2">•</span>
                                <span>{config.usage_count} uses</span>
                            </div>
                        </div>
                    </div>

                    {/* Groups with Hierarchy Visualization */}
                    <div className="flex items-start space-x-2">
                        <div className="min-w-0 flex-1 min-h-0">
                            <span className="text-xs text-muted block mb-1">Access</span>
                            <GroupTreeDisplay
                                groups={config.groups || []}
                                variant="expanded"
                                maxVisible={5}
                            />
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
};

export default ServerConfigRow;